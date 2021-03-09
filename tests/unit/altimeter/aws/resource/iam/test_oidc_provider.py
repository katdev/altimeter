import boto3
from botocore.exceptions import ClientError
from unittest import TestCase
from moto import mock_iam
from unittest.mock import patch
from altimeter.aws.resource.iam.iam_oidc_provider import IAMOIDCProviderResourceSpec
from altimeter.aws.scan.aws_accessor import AWSAccessor


class TestIAMOIDCProvider(TestCase):
    @mock_iam
    def test_disappearing_oidc_provider_race_condition(self):
        account_id = "123456789012"
        oidc_url = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
        oidc_client_ids = ["sts.amazonaws.com"]
        oidc_thumbprints = ["9999999999999999999999999999999999999999"]
        region_name = "us-east-1"

        session = boto3.Session()

        client = session.client("iam")

        oidc_provider_resp = client.create_open_id_connect_provider(
            Url=oidc_url, ClientIDList=oidc_client_ids, ThumbprintList=oidc_thumbprints,
        )
        oidc_provider_arn = oidc_provider_resp["OpenIDConnectProviderArn"]

        scan_accessor = AWSAccessor(session=session, account_id=account_id, region_name=region_name)
        with patch(
            "altimeter.aws.resource.iam.iam_oidc_provider.IAMOIDCProviderResourceSpec.get_oidc_provider_details"
        ) as mock_get_oidc_provider_details:
            mock_get_oidc_provider_details.side_effect = ClientError(
                operation_name="GetOIDCProvider",
                error_response={
                    "Error": {
                        "Code": "NoSuchEntity",
                        "Message": f"OpenIDConnect Provider not found for arn {oidc_provider_arn}",
                    }
                },
            )
            resources = IAMOIDCProviderResourceSpec.scan(scan_accessor=scan_accessor)
            self.assertEqual(resources, [])
