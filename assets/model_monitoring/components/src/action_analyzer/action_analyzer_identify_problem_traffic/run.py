# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Entry script for Action Analyzer identify problem traffic."""

import argparse
import json
import yaml
from pyspark.sql.functions import col, lit, udf, when, concat, explode
from pyspark.sql.types import (
    StructType,
    StructField,
    StringType,
    ArrayType,
    FloatType
)
from shared_utilities.constants import (
    GSQ_METRICS_LIST,
    METRICS_VIOLATION_THRESHOLD,
    RETRIEVAL_SPAN_TYPE,
    TEXT_SPLITTER,
    ACTION_ANALYZER_SAMPLE_SIZE,
    PROMPT_COLUMN,
    COMPLETION_COLUMN,
    CONTEXT_COLUMN,
    TRACE_ID_COLUMN,
    SPAN_ID_COLUMN,
    ROOT_QUESTION_COLUMN,
    TOPIC_LIST_COLUMN,
    GROUP_LIST_COLUMN,
    VIOLATED_METRICS_COLUMN,
    INDEX_CONTENT_COLUMN,
    INDEX_SCORE_COLUMN,
    INDEX_ID_COLUMN,
    ROOT_SPAN_COLUMN,
    GROUP_TOPIC_MIN_SAMPLE_SIZE
)
from shared_utilities.prompts import BERTOPIC_DEFAULT_PROMPT
from shared_utilities.span_tree_utils import SpanTree
from model_data_collector_preprocessor.store_url import StoreUrl

from shared_utilities.io_utils import (
    try_read_mltable_in_spark,
    save_spark_df_as_mltable,
    save_empty_dataframe
)
from shared_utilities.llm_utils import (
    API_KEY,
    _WorkspaceConnectionTokenManager
)

from bertopic import BERTopic
from openai import AzureOpenAI
from bertopic.representation import OpenAI


def get_output_schema() -> StructType:
    """Get Output Data Spark DataFrame Schema."""
    schema = StructType(
        [
            StructField(TRACE_ID_COLUMN, StringType(), True),
            StructField(SPAN_ID_COLUMN, StringType(), True),
            StructField(ROOT_QUESTION_COLUMN, StringType(), True),
            StructField(PROMPT_COLUMN, StringType(), True),
            StructField(COMPLETION_COLUMN, StringType(), True),
            StructField(TOPIC_LIST_COLUMN, StringType(), True),
            StructField(GROUP_LIST_COLUMN, StringType(), True),
            StructField(VIOLATED_METRICS_COLUMN, StringType(), True),
            StructField(INDEX_CONTENT_COLUMN, StringType(), True),
            StructField(INDEX_ID_COLUMN, StringType(), True),
            StructField(CONTEXT_COLUMN, StringType(), True),
            StructField(INDEX_SCORE_COLUMN, FloatType(), True)
        ]
    )
    return schema


def bertopic_get_topic(queries,
                       workspace_connection_arm_id,
                       model_deployment_name):
    """Group queries in semantic groups using Bertopic."""
    token_manager = _WorkspaceConnectionTokenManager(connection_name=workspace_connection_arm_id,
                                                     auth_header=API_KEY)
    azure_endpoint_domain_name = token_manager.get_endpoint_domain()
    azure_openai_api_version = token_manager.get_api_version()
    azure_openai_api_key = token_manager.get_token()
    client = AzureOpenAI(api_version=azure_openai_api_version,
                         api_key=azure_openai_api_key,
                         azure_endpoint=azure_endpoint_domain_name,
                         azure_deployment=model_deployment_name)
    representation_model = OpenAI(client, model=model_deployment_name, chat=True, prompt=BERTOPIC_DEFAULT_PROMPT)
    topic_model = BERTopic(
        min_topic_size=3,
        top_n_words=5,
        representation_model=representation_model
    )
    topics, probs = topic_model.fit_transform(queries)

    docs = topic_model.get_document_info(queries)
    docs['Representation'] = docs['Representation'].str.get(0)
    doc_per_topic = docs.groupby('Representation')['Document'].agg(lambda x: list(x)).reset_index()
    topics_df = doc_per_topic.set_index('Representation')
    topics_dict = topics_df.to_dict()["Document"]

    print("Get topic dictionary: ")
    for k, v in topics_dict.items():
        print("Topic: ")
        print(k)
        print("\n")
        print("Questions: ", len(v))
        print("\t", "\n\t".join(v))
        print("\n")
    return topics_dict


def _append_value(string_input, value):
    if string_input == "":
        return value
    else:
        string_set = set(string_input.split(TEXT_SPLITTER))
        string_set.add(value)
        return TEXT_SPLITTER.join(string_set)


@udf(returnType=ArrayType(StringType()))
def assign_topic_and_group(topic_list, group_list, question, violated_metrics, metrics, topic_group_dict):
    """Assign topic name and group name for bad queries."""
    topic_group = json.loads(topic_group_dict)
    for group_name, (topic, q_list) in topic_group.items():
        if question in q_list and (metrics in violated_metrics):
            topic_list = _append_value(topic_list, topic)
            group_list = _append_value(group_list, group_name)
    return (topic_list, group_list)


@udf(returnType=StringType())
def assign_good_topic(topic_list, question, metrics_score, topics_dict):
    """Assign topic name for good queries."""
    topic_question = json.loads(topics_dict)
    for topic, q_list in topic_question.items():
        if question in q_list and metrics_score == 5:
            topic_list = _append_value(topic_list, topic)
    return topic_list


def get_index_id(index_content):
    """Parse the index id from index yaml."""
    index_payload = yaml.safe_load(index_content)
    index = index_payload['index']
    # if the asset id does not exist, use the index name
    if "self" in index:
        index_id = index["self"].get("asset_id", None)
    elif "index" in index:
        index_id = index["index"]
    else:
        index_id = None
    return index_id


@udf(returnType=ArrayType(StructType([
    StructField(SPAN_ID_COLUMN, StringType()),
    StructField(INDEX_CONTENT_COLUMN, StringType()),
    StructField(INDEX_ID_COLUMN, StringType()),
    StructField(PROMPT_COLUMN, StringType()),
    StructField(CONTEXT_COLUMN, StringType()),
    StructField(INDEX_SCORE_COLUMN, FloatType())])))
def parse_debugging_info(root_span):
    """Parse the span tree to get debugging info."""
    try:
        tree = SpanTree.create_tree_from_json_string(root_span)
        spans_array = []
        for span in tree:
            if span.span_type == RETRIEVAL_SPAN_TYPE:
                parent_id = span.parent_id
                if not parent_id:
                    print("No look up span found, skip action analyzer.")
                    return None
                index_span = tree.get_span_tree_node_by_span_id(parent_id)
                index_input = json.loads(json.loads(index_span.attributes)["inputs"])
                index_content = index_input['mlindex_content']
                index_id = get_index_id(index_content)
                retrieval_info = json.loads(span.attributes)
                query = retrieval_info["retrieval.query"]
                retrieval_documents = json.loads(retrieval_info["retrieval.documents"])
                text = []
                score = []
                for document in retrieval_documents:
                    text.append(document["document.content"])
                    score.append(float(document["document.score"]))
                spans_array.append((parent_id, index_content, index_id, query, TEXT_SPLITTER.join(text), max(score)))
        return spans_array
    except KeyError as e:
        print("Required field not found: ", e)
        return None


def convert_to_span_level(df):
    """Convert the dataframe from trace level to span level."""
    debugging_details = parse_debugging_info(col(ROOT_SPAN_COLUMN))
    if debugging_details is None:
        return df
    df = df.withColumn("debugging_info", debugging_details)
    df_exploaded = df.withColumn("debugging_details", explode("debugging_info")).drop("debugging_info")
    span_level_df = df_exploaded.withColumn(SPAN_ID_COLUMN, col(f"debugging_details.{SPAN_ID_COLUMN}"))\
                                .withColumn(INDEX_ID_COLUMN, col(f"debugging_details.{INDEX_ID_COLUMN}"))\
                                .withColumn(INDEX_CONTENT_COLUMN, col(f"debugging_details.{INDEX_CONTENT_COLUMN}"))\
                                .withColumn(PROMPT_COLUMN, col(f"debugging_details.{PROMPT_COLUMN}"))\
                                .withColumn(CONTEXT_COLUMN, col(f"debugging_details.{CONTEXT_COLUMN}"))\
                                .withColumn(INDEX_SCORE_COLUMN, col(f"debugging_details.{INDEX_SCORE_COLUMN}"))\
                                .drop("debugging_details")
    return span_level_df


@udf(returnType=StringType())
def assign_violated_metrics(violated_metrics, metric_score, metrics):
    """Add violated metrics name."""
    if (metric_score < METRICS_VIOLATION_THRESHOLD):
        violated_metrics = _append_value(violated_metrics, metrics)
    return violated_metrics


def get_violated_metrics(signal_out_url, signal_name):
    """Get the violated metrics names from the gsq output."""
    violated_metrics = []
    try:
        store_url = StoreUrl(signal_out_url)
        gsq_output = store_url.read_file_content(f"{signal_name}.json")
        gsq_output_json = json.loads(gsq_output)
        metrics_dict = gsq_output_json["metrics"]
        for metrics in GSQ_METRICS_LIST:
            pass_rate_metrics = f"Aggregated{metrics}PassRate"
            if pass_rate_metrics in metrics_dict:
                if metrics_dict[pass_rate_metrics]["value"] < metrics_dict[pass_rate_metrics]["threshold"]:
                    print(f"Metrics {metrics} violated.")
                    violated_metrics.append(metrics)
        return violated_metrics
    except Exception as e:
        print("Exception while getting the violated metrics. ", e)
        return []


def run():
    """Identify problem traffic."""
    # Parse argument
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_with_groups", type=str)
    parser.add_argument("--signal_scored_data", type=str)
    parser.add_argument("--signal_output", type=str)
    parser.add_argument("--signal_name", type=str)
    parser.add_argument("--model_deployment_name", type=str, required=True)
    parser.add_argument("--workspace_connection_arm_id", type=str, required=True)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top_p", type=float, default=1.0)
    parser.add_argument("--num_samples", type=int, default=1)
    parser.add_argument("--frequency_penalty", type=float, default=0.0)
    parser.add_argument("--presence_penalty", type=float, default=0.0)
    parser.add_argument("--stop", type=str, default=None)
    parser.add_argument("--api_call_retry_backoff_factor", type=int, default=4)
    parser.add_argument("--api_call_retry_max_count", type=int, default=10)
    parser.add_argument("--prompt_column_name", type=str, default=PROMPT_COLUMN)
    parser.add_argument("--completion_column_name", type=str, default=COMPLETION_COLUMN)
    args = parser.parse_args()

    violated_metrics = get_violated_metrics(args.signal_output, f"signals/{args.signal_name}")
    if violated_metrics == []:
        print("No violated metrics. No action will be generated.")
        save_empty_dataframe(get_output_schema(), args.data_with_groups)
        return

    print("Violated metrics found: ", violated_metrics)

    signal_scored_data_df = try_read_mltable_in_spark(args.signal_scored_data, "signal_scored_data")
    print("gsq output df")
    signal_scored_data_df.show()

    df = signal_scored_data_df.withColumn(TOPIC_LIST_COLUMN, lit("")) \
                              .withColumn(GROUP_LIST_COLUMN, lit("")) \
                              .withColumn(VIOLATED_METRICS_COLUMN, lit(""))

    # rename to root question column
    df = df.withColumn(ROOT_QUESTION_COLUMN, col(PROMPT_COLUMN)).drop(PROMPT_COLUMN)
    # seperate bad groups with semantic topic
    for metrics in violated_metrics:
        print("======Current metrics=====")
        print(metrics)
        score_name = metrics
        df = df.withColumn(VIOLATED_METRICS_COLUMN,
                           assign_violated_metrics(col(VIOLATED_METRICS_COLUMN), col(score_name), lit(metrics)))

        # add good group and bad default group
        good_group_name = f"{metrics}_good_group"
        default_bad_group_name = f"{metrics}_bad_group_default"
        df = df.withColumn(GROUP_LIST_COLUMN,
                           when((col(score_name) == 5) & (col(GROUP_LIST_COLUMN) == ""), good_group_name)
                          .when((col(score_name) == 5) & (col(GROUP_LIST_COLUMN) != ""), concat(col(GROUP_LIST_COLUMN), lit(TEXT_SPLITTER), lit(good_group_name))) # noqa
                          .when((col(score_name) < METRICS_VIOLATION_THRESHOLD) & (col(GROUP_LIST_COLUMN) == ""), default_bad_group_name) # noqa
                          .when((col(score_name) < METRICS_VIOLATION_THRESHOLD) & (col(GROUP_LIST_COLUMN) != ""), concat(col(GROUP_LIST_COLUMN), lit(TEXT_SPLITTER), lit(default_bad_group_name)))  # noqa
                          .otherwise(col(GROUP_LIST_COLUMN)))  # noqa

        print("Start to do semantic grouping")
        df.show()
        pdf = df.toPandas()
        bad_answers = pdf[pdf[score_name] < METRICS_VIOLATION_THRESHOLD]
        bad_samples = bad_answers.sample(n=min(ACTION_ANALYZER_SAMPLE_SIZE, len(bad_answers)))
        good_answers = pdf[pdf[score_name] == 5]
        # sample good samples to have same size as bad samples
        # good_samples = good_answers.sample(n=min(N_SAMPLES, len(bad_answers)))
        good_samples = good_answers

        # add semantic groups for bad queries
        if len(bad_samples) > GROUP_TOPIC_MIN_SAMPLE_SIZE:
            print("add semantic groups for bad queries")
            topics_dict = bertopic_get_topic(bad_samples[ROOT_QUESTION_COLUMN].tolist(),
                                             args.workspace_connection_arm_id,
                                             args.model_deployment_name)

            topic_group_dict = {f"{metrics}_bad_group_{i}_{k}": (k, v) for i, (k, v) in enumerate(topics_dict.items())}
            topic_group_columns = assign_topic_and_group(col(TOPIC_LIST_COLUMN),
                                                         col(GROUP_LIST_COLUMN),
                                                         col(ROOT_QUESTION_COLUMN),
                                                         col(VIOLATED_METRICS_COLUMN),
                                                         lit(metrics),
                                                         lit(json.dumps(topic_group_dict)))

            df = df.withColumn(TOPIC_LIST_COLUMN, topic_group_columns[0])
            df = df.withColumn(GROUP_LIST_COLUMN, topic_group_columns[1])

        # add semantic groups for good queries
        if len(good_samples) > GROUP_TOPIC_MIN_SAMPLE_SIZE:
            print("add semantic groups for good queries")
            topics_dict = bertopic_get_topic(good_samples[ROOT_QUESTION_COLUMN].tolist(),
                                             args.workspace_connection_arm_id,
                                             args.model_deployment_name)

            df = df.withColumn(TOPIC_LIST_COLUMN, assign_good_topic(col(TOPIC_LIST_COLUMN),
                                                                    col(ROOT_QUESTION_COLUMN),
                                                                    col(score_name),
                                                                    lit(json.dumps(topics_dict))))

    sampled_df = df.filter(col(TOPIC_LIST_COLUMN) != "")
    sampled_df.show()
    span_level_df = convert_to_span_level(sampled_df)
    save_spark_df_as_mltable(span_level_df, args.data_with_groups)


if __name__ == "__main__":
    run()
