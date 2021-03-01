#!/usr/bin/env python3
"""Execute all known QJs, run the query portion of a QJ, and prune results according to Job config settings"""
import hashlib
import json
from typing import Any, Dict, List
import uuid

import boto3
from pydantic import ValidationError

from altimeter.core.log import Logger
from altimeter.core.neptune.client import AltimeterNeptuneClient, NeptuneEndpoint
from altimeter.core.neptune.results import QueryResult
from altimeter.qj import schemas
from altimeter.qj.client import QJAPIClient
from altimeter.qj.config import ExecutorConfig
from altimeter.qj.config import QueryConfig
from altimeter.qj.config import PrunerConfig
from altimeter.qj.log import QJLogEvents
from altimeter.qj.security import get_api_key


class InvalidLambdaInputException(Exception):
    """Indicates the input to the queryjob lambda is invalid"""


def lambda_handler(event: Dict[str, Any], _: Any) -> None:
    """Lambda entrypoint"""
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers:
            root.removeHandler(handler)
    try:
        # Execute all known QJs
        # if this was triggered by an sns message, use that message as part of the
        # deduplication id for each sqs message.  Otherwise generate a unique so that
        # repeated manual runs of executor will not be dedupe'd
        sns_message = event.get("Records", [{}])[0].get("Sns", {}).get("Message")
        if sns_message:
            execution_hash = hashlib.sha256(sns_message.encode()).hexdigest()
        else:
            execution_hash = hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest()
        config = ExecutorConfig()
        logger = Logger()
        logger.info(
            event=QJLogEvents.InitConfig,
            sns_triggered=bool(sns_message),
            execution_hash=execution_hash,
        )
        qj_client = QJAPIClient(host=config.api_host, port=config.api_port)
        jobs = qj_client.get_jobs(active_only=True)
        logger.info(event=QJLogEvents.GetJobs, num_jobs=len(jobs))
        enqueue_queries(
            jobs=jobs,
            queue_url=config.query_queue_url,
            execution_hash=execution_hash,
            region=config.region,
        )
    except ValidationError:
        pass
    try:
        # Run the query portion of a QJ
        config = QueryConfig()
        logger = Logger()
        logger.info(event=QJLogEvents.InitConfig, config=config)

        records = event.get("Records", [])
        if not records:
            raise Exception("No records found")
        if len(records) > 1:
            raise Exception(f"More than one record. BatchSize is probably not 1. event: {event}")
        body = records[0].get("body")
        if body is None:
            raise Exception(f"No record body found. BatchSize is probably not 1. event: {event}")
        body = json.loads(body)
        job = schemas.Job(**body)
        logger.info(event=QJLogEvents.InitJob, job=job)

        logger.info(event=QJLogEvents.RunQueryStart)
        query_result = run_query(job=job, config=config)
        logger.info(event=QJLogEvents.RunQueryEnd, num_results=query_result.get_length())

        results: List[schemas.Result] = []
        if config.account_id_key not in query_result.query_result_set.fields:
            raise Exception(f"Query results must contain field '{config.account_id_key}'")
        for q_r in query_result.to_list():
            account_id = q_r[config.account_id_key]
            result = schemas.Result(
                account_id=account_id,
                result={key: val for key, val in q_r.items() if key != config.account_id_key},
            )
            results.append(result)

        graph_spec = schemas.ResultSetGraphSpec(
            graph_uris_load_times=query_result.graph_uris_load_times
        )
        result_set = schemas.ResultSetCreate(job=job, graph_spec=graph_spec, results=results)

        api_key = get_api_key(region_name=config.region)
        qj_client = QJAPIClient(host=config.api_host, port=config.api_port, api_key=api_key)
        logger.info(event=QJLogEvents.CreateResultSetStart)
        qj_client.create_result_set(result_set=result_set)
        logger.info(event=QJLogEvents.CreateResultSetEnd)
    except ValidationError:
        pass
    try:  # Prune results according to Job config settings
        logger = Logger()
        config = PrunerConfig()
        logger.info(event=QJLogEvents.InitConfig, config=config)
        api_key = get_api_key(region_name=config.region)
        qj_client = QJAPIClient(host=config.api_host, port=config.api_port, api_key=api_key)
        logger.info(event=QJLogEvents.DeleteStart)
        result = qj_client.delete_expired_result_sets()
        logger.info(event=QJLogEvents.DeleteEnd, result=result)
    except ValidationError:
        pass
    raise InvalidLambdaInputException(f"Invalid lambda input.\nENV: {os.environ}\nEvent: {event}")


def enqueue_queries(
    jobs: List[schemas.Job], queue_url: str, execution_hash: str, region: str
) -> None:
    """Enqueue querys by sending a message for each job key to queue_url"""
    sqs_client = boto3.client("sqs", region_name=region)
    logger = Logger()
    with logger.bind(queue_url=queue_url, execution_hash=execution_hash):
        for job in jobs:
            job_hash = hashlib.sha256()
            job_hash.update(json.dumps(job.json()).encode())
            message_group_id = job_hash.hexdigest()
            job_hash.update(execution_hash.encode())
            message_dedupe_id = job_hash.hexdigest()
            logger.info(
                QJLogEvents.ScheduleJob,
                job=job,
                message_group_id=message_group_id,
                message_dedupe_id=message_dedupe_id,
            )
            sqs_client.send_message(
                QueueUrl=queue_url,
                MessageBody=job.json(),
                MessageGroupId=message_group_id,
                MessageDeduplicationId=message_dedupe_id,
            )


def run_query(job: schemas.Job, config: QueryConfig) -> QueryResult:
    """Run a query and return a QueryResult object"""
    endpoint = NeptuneEndpoint(
        host=config.neptune_host, port=config.neptune_port, region=config.neptune_region
    )
    neptune_client = AltimeterNeptuneClient(
        max_age_min=int(job.max_graph_age_sec / 60.0), neptune_endpoint=endpoint
    )
    query_result = neptune_client.run_query(
        graph_names=set(job.graph_spec.graph_names), query=job.query
    )
    return query_result
