import json
import os
import boto3
import sys
import argparse
import pprint
import time
from retrying import retry
from opensearchpy import RequestError
from botocore.exceptions import ClientError

sys.path.append("..")  # Add the parent directory to the Python path
from utils.knowledge_bases_roles import interactive_sleep, KnowledgeBaseRoles, KBInfo


class NotSupportedRegionException(Exception):
    """
    Thrown when th script is started with a region name that is not supported by the service
    """

    pass


class CreateKB:
    """
    Creates Bedrock KnoweldgeBase with all the required steps
    Args:
        region_name (str): name of the AWS region
        bucket_name (str): name of the S3 bucket that will be used as a data source
        index_name (str): name of the OpenSearch index
        kb_name (str): name of the KnowledgeBase data source
        vector_store_name (str): name of the vector stote
    """

    def __init__(
        self,
        region_name: str,
        bucket_name: str,
        index_name: str,
        kb_name: str,
        vector_store_name: str,
    ) -> None:
        self.region_name = region_name
        self.bucket_name = bucket_name
        self.index_name = index_name
        self.kb_name = kb_name
        self.vector_store_name = vector_store_name
        self.printer = pprint.PrettyPrinter(indent=2)
        self.kb_roles = KnowledgeBaseRoles(region_name)
        self.kb_info = KBInfo(
            index_name=self.index_name,
            bucket_name=self.bucket_name,
            region_name=self.region_name,
            bedrock_execution_role_name=self.kb_roles.bedrock_execution_role_name,
            fm_policy_name=self.kb_roles.fm_policy_name,
            s3_policy_name=self.kb_roles.s3_policy_name,
            oss_policy_name=self.kb_roles.oss_policy_name,
        )

    def create_bucket(self, s3_client: boto3.client) -> None:
        """
        Creates an S3 bucket if not existing

        Args:
            s3_client (boto3.client): The boto3 client for S3 service.
        """
        try:
            s3_client.head_bucket(Bucket=self.bucket_name)
            print(f"Bucket {self.bucket_name} Exists")
        except ClientError as e:
            print(f"Creating bucket {self.bucket_name}")
            s3_client.create_bucket(
                Bucket=self.bucket_name,
                CreateBucketConfiguration={"LocationConstraint": self.region_name},
            )

    def upload_directory(self, path: str) -> None:
        """
        Upload all files in the given directory to the specified S3 bucket.

        Args:
            path (str): The path to the directory containing the files to upload.
        """
        s3_client = boto3.client("s3")
        for root, _, files in os.walk(path):
            for file in files:
                s3_client.upload_file(os.path.join(root, file), self.bucket_name, file)

    def create_os_polices_and_collection(
        self,
        aoss_client: boto3.client,
    ) -> tuple[dict, str, str]:
        """
        Create security, network, and data access policies within OpenSearch Serverless (OSS),
        and create an OSS collection for the vector store.

        Args:
            aoss_client (boto3.client): The boto3 client for OpenSearch Serverless.

        Returns:
            tuple[dict, str, str]: A tuple containing the created collection, collection ID, and Bedrock execution role ARN.
        """
        bedrock_kb_execution_role = self.kb_roles.create_bedrock_execution_role(
            bucket_name=self.bucket_name
        )
        bedrock_kb_execution_role_arn = bedrock_kb_execution_role["Role"]["Arn"]
        # create security, network and data access policies within OSS
        encryption_policy, network_policy, access_policy = (
            self.kb_roles.create_policies_in_oss(
                vector_store_name=self.vector_store_name,
                aoss_client=aoss_client,
                bedrock_kb_execution_role_arn=bedrock_kb_execution_role_arn,
            )
        )
        self.kb_info.access_policy_name = access_policy["accessPolicyDetail"]["name"]
        self.kb_info.network_policy_name = network_policy["securityPolicyDetail"][
            "name"
        ]
        self.kb_info.encryption_policy_name = encryption_policy["securityPolicyDetail"][
            "name"
        ]

        collection = aoss_client.create_collection(
            name=self.vector_store_name, type="VECTORSEARCH"
        )
        self.printer.pprint(collection)

        # Get the OpenSearch serverless collection URL
        collection_id = collection["createCollectionDetail"]["id"]
        host = collection_id + "." + self.region_name + ".aoss.amazonaws.com"
        print(host)
        self.kb_info.collection_id = collection_id
        # wait for collection creation
        # This can take couple of minutes to finish
        response = aoss_client.batch_get_collection(names=[self.vector_store_name])
        # Periodically check collection status
        while (response["collectionDetails"][0]["status"]) == "CREATING":
            print("Creating collection...")
            interactive_sleep(30)
            response = aoss_client.batch_get_collection(names=[self.vector_store_name])
        print("\nCollection successfully created:")
        self.printer.pprint(response["collectionDetails"])

        # create opensearch serverless access policy and attach it to Bedrock execution role
        try:
            self.kb_roles.create_oss_policy_attach_bedrock_execution_role(
                collection_id=collection_id,
                bedrock_kb_execution_role=bedrock_kb_execution_role,
            )
            # It can take up to a minute for data access rules to be enforced
            interactive_sleep(60)
        except Exception as e:
            print("Policy already exists")
            self.printer.pprint(e)
        return collection, collection_id, bedrock_kb_execution_role_arn

    def create_vector_index(
        self,
        collection_id: str,
    ) -> None:
        """
        Create a vector index in OpenSearch Serverless with the knn_vector field index mapping,
        specifying the dimension size, name, and engine.

        Args:
            collection_id (str): The ID of the OpenSearch Serverless collection.
        """
        oss_client = self.kb_roles.create_os_client(collection_id)

        body_json = {
            "settings": {
                "index.knn": "true",
                "number_of_shards": 1,
                "knn.algo_param.ef_search": 512,
                "number_of_replicas": 0,
            },
            "mappings": {
                "properties": {
                    "vector": {
                        "type": "knn_vector",
                        "dimension": 1536,
                        "method": {
                            "name": "hnsw",
                            "engine": "faiss",
                            "space_type": "l2",
                        },
                    },
                    "text": {"type": "text"},
                    "text-metadata": {"type": "text"},
                }
            },
        }

        # Create index
        try:
            response = oss_client.indices.create(
                index=self.index_name, body=json.dumps(body_json)
            )
            print("\nCreating index:")
            self.printer.pprint(response)

            # index creation can take up to a minute
            interactive_sleep(60)
        except RequestError as e:
            # you can delete the index if its already exists
            # oss_client.indices.delete(index=index_name)
            print(
                f"Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to delete, and recreate the index"
            )

    def create_knowledge_base(
        self,
        collection: dict,
        bedrock_kb_execution_role_arn: str,
        bedrock_agent_client: boto3.client,
    ) -> tuple[dict, dict]:
        """
        Create a Knowledge Base and a Data Source within the Knowledge Base.

        Args:
            collection (dict): The created OpenSearch Serverless collection.
            bedrock_kb_execution_role_arn (str): The ARN of the Bedrock execution role.
            bedrock_agent_client (boto3.client): The boto3 client for Bedrock Agent.

        Returns:
            tuple[dict, dict]: A tuple containing the created Knowledge Base and Data Source.
        """

        @retry(wait_random_min=1000, wait_random_max=2000, stop_max_attempt_number=7)
        def create_knowledge_base_func():
            create_kb_response = bedrock_agent_client.create_knowledge_base(
                name=self.kb_name,
                description=description,
                roleArn=roleArn,
                knowledgeBaseConfiguration={
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn": embeddingModelArn
                    },
                },
                storageConfiguration={
                    "type": "OPENSEARCH_SERVERLESS",
                    "opensearchServerlessConfiguration": opensearchServerlessConfiguration,
                },
            )
            return create_kb_response["knowledgeBase"]

        opensearchServerlessConfiguration = {
            "collectionArn": collection["createCollectionDetail"]["arn"],
            "vectorIndexName": self.index_name,
            "fieldMapping": {
                "vectorField": "vector",
                "textField": "text",
                "metadataField": "text-metadata",
            },
        }

        # Ingest strategy - How to ingest data from the data source
        chunkingStrategyConfiguration = {
            "chunkingStrategy": "FIXED_SIZE",
            "fixedSizeChunkingConfiguration": {
                "maxTokens": 512,
                "overlapPercentage": 20,
            },
        }

        # The data source to ingest documents from, into the OpenSearch serverless knowledge base index
        s3Configuration = {
            "bucketArn": f"arn:aws:s3:::{self.bucket_name}",
            # "inclusionPrefixes":["*.*"] # you can use this if you want to create a KB using data within s3 prefixes.
        }

        # The embedding model used by Bedrock to embed ingested documents, and realtime prompts
        embeddingModelArn = f"arn:aws:bedrock:{self.region_name}::foundation-model/amazon.titan-embed-text-v1"

        description = "Amazon shareholder letter knowledge base."
        roleArn = bedrock_kb_execution_role_arn

        try:
            kb = create_knowledge_base_func()
        except Exception as err:
            print(f"{err=}, {type(err)=}")
        self.printer.pprint(kb)

        # Create a DataSource in KnowledgeBase
        create_ds_response = bedrock_agent_client.create_data_source(
            name=self.kb_name,
            description=description,
            knowledgeBaseId=kb["knowledgeBaseId"],
            dataSourceConfiguration={"type": "S3", "s3Configuration": s3Configuration},
            vectorIngestionConfiguration={
                "chunkingConfiguration": chunkingStrategyConfiguration
            },
        )
        ds = create_ds_response["dataSource"]
        self.printer.pprint(ds)
        bedrock_agent_client.get_data_source(
            knowledgeBaseId=kb["knowledgeBaseId"], dataSourceId=ds["dataSourceId"]
        )
        self.kb_info.kb_id = kb["knowledgeBaseId"]
        self.kb_info.ds_id = ds["dataSourceId"]
        return kb, ds

    def start_ingestion_job(
        self, bedrock_agent_client: boto3.client, kb: dict, ds: dict
    ) -> None:
        """
        Start an ingestion job for the created Data Source within the Knowledge Base.

        Args:
            bedrock_agent_client (boto3.client): The boto3 client for Bedrock Agent.
            kb (dict): The created Knowledge Base.
            ds (dict): The created Data Source.
        """

        start_job_response = bedrock_agent_client.start_ingestion_job(
            knowledgeBaseId=kb["knowledgeBaseId"], dataSourceId=ds["dataSourceId"]
        )
        job = start_job_response["ingestionJob"]
        self.printer.pprint(job)

        # Get job
        while job["status"] != "COMPLETE":
            get_job_response = bedrock_agent_client.get_ingestion_job(
                knowledgeBaseId=kb["knowledgeBaseId"],
                dataSourceId=ds["dataSourceId"],
                ingestionJobId=job["ingestionJobId"],
            )
            job = get_job_response["ingestionJob"]
            time.sleep(5)
        self.printer.pprint(job)
        interactive_sleep(40)

        # Print the knowledge base Id in bedrock, that corresponds to the Opensearch index in the collection we created before, we will use it for the invocation later
        kb_id = kb["knowledgeBaseId"]
        self.printer.pprint(kb_id)


def main():
    parser = argparse.ArgumentParser(
        description="Create and ingest documents into a knowledge base, by default files will be copied from ../data directory to the provided S3 location"
    )
    parser.add_argument(
        "--region_name",
        type=str,
        required=False,
        help="AWS region name",
        default="us-west-2",
    )
    parser.add_argument(
        "--knowledge_base_name", type=str, required=True, help="Knowledge base name"
    )
    parser.add_argument(
        "--bucket_name",
        type=str,
        required=False,
        help="S3 Bucket name that should save the data",
    )
    parser.add_argument(
        "--use_s3",
        required=False,
        help="If set, we use the files in the provided S3 location without copying ../data location",
        default=False,
    )
    parser.add_argument(
        "--vectorstore_name",
        type=str,
        required=False,
        help="Name of the vector store",
        default="bedrock-sample-rag-vs",
    )
    parser.add_argument(
        "--index_name",
        type=str,
        required=False,
        help="Name of the opensearch index",
        default="bedrock-sample-rag-index",
    )

    args = parser.parse_args()

    region_name = args.region_name
    if region_name not in ["us-east-1", "us-west-2"]:
        raise NotSupportedRegionException(
            "The region for needs to be sit to us-east-1 or us-west-2"
        )

    boto3.setup_default_session(region_name=region_name)
    boto3_session = boto3.session.Session(region_name=region_name)
    bedrock_agent_client = boto3_session.client(
        "bedrock-agent", region_name=region_name
    )
    s3_client = boto3.client("s3")
    aoss_client = boto3_session.client("opensearchserverless")
    s3_suffix = f"{region_name}-{boto3.client('sts').get_caller_identity()['Account']}"
    bucket_name = (
        f"bedrock-kb-{s3_suffix}" if not args.bucket_name else args.bucket_name
    )
    vector_store_name = args.vectorstore_name
    index_name = args.index_name
    knowledge_base_name = args.knowledge_base_name
    kb_instance = CreateKB(
        region_name, bucket_name, index_name, knowledge_base_name, vector_store_name
    )

    # Step 1: Create an S3 bucket if not existing
    kb_instance.create_bucket(s3_client)

    # Step 2: Create OSS policies and collection
    collection, collection_id, bedrock_kb_execution_role_arn = (
        kb_instance.create_os_polices_and_collection(aoss_client)
    )

    # Step 3: Create vector index
    # Create the vector index in Opensearch serverless, with the knn_vector field index mapping, specifying the dimension size, name and engine.
    kb_instance.create_vector_index(collection_id)

    # Upload data to s3 to the bucket that was configured as a data source to the knowledge base
    if not args.use_s3:
        kb_instance.upload_directory("../data")

    # Step 4: Create Knowledge Base
    kb, ds = kb_instance.create_knowledge_base(
        collection, bedrock_kb_execution_role_arn, bedrock_agent_client
    )

    # Step 5: Start an ingestion job
    kb_instance.start_ingestion_job(bedrock_agent_client, kb, ds)

    with open("kb_info.json", "w") as file:
        json.dump(
            kb_instance.kb_info.model_dump(), file, indent=4
        )  # indent=4 for pretty-printing


if __name__ == "__main__":
    main()
