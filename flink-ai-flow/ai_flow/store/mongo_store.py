#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
import logging
import time
from typing import Optional, Text, List, Union

import mongoengine

from ai_flow.common.status import Status
from ai_flow.meta.artifact_meta import ArtifactMeta
from ai_flow.meta.example_meta import ExampleMeta, Properties, DataType, Schema, ExampleSupportType
from ai_flow.meta.job_meta import JobMeta, State
from ai_flow.meta.metric_meta import MetricMeta, MetricType, MetricSummary
from ai_flow.meta.model_relation_meta import ModelRelationMeta, ModelVersionRelationMeta
from ai_flow.meta.project_meta import ProjectMeta
from ai_flow.meta.workflow_execution_meta import WorkflowExecutionMeta
from ai_flow.metadata_store.utils.MetaToTable import MetaToTable
from ai_flow.metadata_store.utils.ResultToMeta import ResultToMeta
from ai_flow.metric.utils import table_to_metric_meta, table_to_metric_summary, metric_meta_to_table, \
    metric_summary_to_table
from ai_flow.model_center.entity.model_version_stage import STAGE_DELETED, get_canonical_stage, STAGE_GENERATED, \
    STAGE_DEPLOYED, STAGE_VALIDATED
from ai_flow.rest_endpoint.protobuf.message_pb2 import INVALID_PARAMETER_VALUE, RESOURCE_ALREADY_EXISTS
from ai_flow.rest_endpoint.service.exception import AIFlowException
from ai_flow.rest_endpoint.service.high_availability import Member
from ai_flow.store import MONGO_DB_ALIAS_META_SERVICE
from ai_flow.store.abstract_store import AbstractStore
from ai_flow.store.db.db_model import (MongoProject, MongoExample, MongoModelVersion, MongoJob,
                                       MongoArtifact, MongoRegisteredModel, MongoModelRelation,
                                       MongoWorkflowExecution, MongoMetricSummary, MongoMetricMeta,
                                       MongoModelVersionRelation, MongoMember)
from ai_flow.store.db.db_util import parse_mongo_uri

if not hasattr(time, 'time_ns'):
    time.time_ns = lambda: int(time.time() * 1e9)

_logger = logging.getLogger(__name__)

TRUE = True
UPDATE_FAIL = -1
deleted_character = '~~'


class MongoStore(AbstractStore):
    """
    MongoDB backend store for tracking metadata for AIFlow backend entities.
    """

    CREATE_RETRY_TIMES = 3

    def __init__(self, **kwargs):
        """
        Create database backend storage by specified database URI

        :param db_uri: The mongodb database URI string to connect to the database.
        """
        super(MongoStore, self).__init__()
        db_conf = {
            "host": kwargs.get("host"),
            "port": kwargs.get("port"),
            "username": kwargs.get("username"),
            "password": kwargs.get("password"),
            "db": kwargs.get("db"),
        }
        self.db_uri = "mongodb://{username}:{password}@{host}:{port}/{db}?authSource=admin".format(**db_conf)
        MongoStoreConnManager().connect(self.db_uri, kwargs.get("db"))

    def _save_all(self, objs):
        """
        Backend entities store into database
        """
        if type(objs) is list:
            for obj in objs:
                obj.save()
        else:
            objs.save()

    """example api"""

    def get_example_by_id(self, example_id) -> Optional[ExampleMeta]:
        """
        get an specific example in metadata store by example id.

        :param example_id: the example id
        :return: A single :py:class:`ai_flow.meta.example_meta.ExampleMeta` object if the example exists,
                 Otherwise, returns None if the example does not exist.
        """
        example_result = MongoExample.objects(uuid=example_id, is_deleted__ne=TRUE)
        if len(example_result) == 0:
            return None
        return ResultToMeta.result_to_example_meta(example_result[0])

    def get_example_by_name(self, example_name) -> Optional[ExampleMeta]:
        """
        get an specific example in metadata store by example name.

        :param example_name: the example name
        :return: A single :py:class:`ai_flow.meta.example_meta.ExampleMeta` object if the example exists,,
                 Otherwise, returns None if the example does not exist.
        """
        example_result = MongoExample.objects(name=example_name, is_deleted__ne=TRUE)
        if len(example_result) == 0:
            return None
        return ResultToMeta.result_to_example_meta(example_result[0])

    def _register_example(self, name: Text, support_type: ExampleSupportType,
                          data_type: Text = None, data_format: Text = None,
                          description: Text = None, batch_uri: Text = None,
                          stream_uri: Text = None, create_time: int = None,
                          update_time: int = None, properties: Properties = None,
                          name_list: List[Text] = None, type_list: List[DataType] = None,
                          catalog_name: Text = None, catalog_type: Text = None,
                          catalog_connection_uri: Text = None, catalog_version: Text = None,
                          catalog_table: Text = None, catalog_database: Text = None) -> ExampleMeta:
        """
        register an example in metadata store.

        :param name: the name of the example
        :param support_type: the example's support_type
        :param data_type: the data type of the example
        :param data_format: the data format of the example
        :param description: the description of the example
        :param batch_uri: the batch uri of the example
        :param stream_uri: the stream uri of the example
        :param create_time: the time when the example is created
        :param update_time: the time when the example is updated
        :param properties: the properties of the example
        :param name_list: the name list of example's schema
        :param type_list: the type list corresponded to the name list of example's schema
        :param catalog_name: the catalog name that will register in environment
        :param catalog_type: the catalog type of the example
        :param catalog_connection_uri: the connection uri of the catalog
        :param catalog_version: the catalog version
        :param catalog_table: the table where the example is stored in the catalog
               if example is stored in the external catalog
        :param catalog_database: the database where the example is stored in the catalog
        :return: A single :py:class:`ai_flow.meta.example_meta.ExampleMeta` object.
        """
        before_example = self.get_example_by_name(example_name=name)
        if before_example is not None:
            # if the user has registered exactly the same example before,
            # do nothing in metadata store and return the registered example.
            if _compare_example_fields(support_type, data_type, data_format, description, batch_uri, stream_uri,
                                       create_time, update_time, properties, name_list, type_list,
                                       catalog_name, catalog_type, catalog_database, catalog_connection_uri,
                                       catalog_table, catalog_version, before_example):
                return before_example
            else:
                # if the example registered this time has the same name but different fields,
                # raise the AIFlowException.
                raise AIFlowException("You have registered the example with same name: \"{}\" "
                                      "but different fields".format(name))
        try:
            if name_list is not None and type_list is not None:
                if len(name_list) != len(type_list):
                    raise AIFlowException("the length of name list and type list should be the same")
            if name_list is not None and type_list is None:
                raise AIFlowException("the length of name list and type list should be the same")
            if name_list is None and type_list is not None:
                raise AIFlowException("the length of name list and type list should be the same")
            example = MetaToTable.example_meta_to_table(name=name, support_type=support_type,
                                                        data_type=data_type, data_format=data_format,
                                                        description=description, batch_uri=batch_uri,
                                                        stream_uri=stream_uri, create_time=create_time,
                                                        update_time=update_time, properties=properties,
                                                        name_list=name_list, type_list=type_list,
                                                        catalog_name=catalog_name, catalog_type=catalog_type,
                                                        catalog_database=catalog_database,
                                                        catalog_connection_uri=catalog_connection_uri,
                                                        catalog_version=catalog_version,
                                                        catalog_table=catalog_table,
                                                        store_type=type(self).__name__)
            example.save()
            schema = Schema(name_list=name_list, type_list=type_list)
            return ExampleMeta(uuid=example.uuid, name=name, data_type=data_type, data_format=data_format,
                               support_type=support_type, description=description, batch_uri=batch_uri,
                               stream_uri=stream_uri, create_time=create_time, update_time=update_time,
                               properties=properties, schema=schema, catalog_name=catalog_name,
                               catalog_type=catalog_type, catalog_database=catalog_database,
                               catalog_connection_uri=catalog_connection_uri, catalog_table=catalog_table,
                               catalog_version=catalog_version)
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered Example (name={}) already exists. '
                                  'Error: {}'.format(example.name, str(e)))

    def register_example(self, name: Text, support_type: ExampleSupportType,
                         data_type: Text = None, data_format: Text = None,
                         description: Text = None, batch_uri: Text = None,
                         stream_uri: Text = None, create_time: int = None,
                         update_time: int = None, properties: Properties = None,
                         name_list: List[Text] = None, type_list: List[DataType] = None):
        """
        register an example in metadata store.

        :param name: the name of the example
        :param support_type: the example's support_type
        :param data_type: the data type of the example
        :param data_format: the data format of the example
        :param description: the description of the example
        :param batch_uri: the batch uri of the example
        :param stream_uri: the stream uri of the example
        :param create_time: the time when the example is created
        :param update_time: the time when the example is updated
        :param properties: the properties of the example
        :param name_list: the name list of example's schema
        :param type_list: the type list corresponded to the name list of example's schema
        :return: A single :py:class:`ai_flow.meta.example_meta.ExampleMeta` object.
        """
        return self._register_example(name=name, support_type=support_type, data_type=data_type,
                                      data_format=data_format, description=description, batch_uri=batch_uri,
                                      stream_uri=stream_uri, create_time=create_time, update_time=update_time,
                                      properties=properties, name_list=name_list, type_list=type_list)

    def register_example_with_catalog(self, name: Text, support_type: ExampleSupportType, catalog_name: Text,
                                      catalog_type: Text, catalog_connection_uri: Text, catalog_version: Text,
                                      catalog_table: Text, catalog_database: Text = None):
        """
        register an example in metadata store with catalog.

        :param name: the name of the example
        :param support_type: the example's support_type
        :param catalog_name: the catalog name that will register in environment
        :param catalog_type: the catalog type of the example
        :param catalog_connection_uri: the connection uri of the catalog
        :param catalog_version: the catalog version
        :param catalog_table: the table where the example is stored in the catalog
        :param catalog_database: the database where the example is stored in the catalog
        :return: A single :py:class:`ai_flow.meta.example_meta.ExampleMeta` object.
        """
        return self._register_example(name=name, support_type=support_type, catalog_name=catalog_name,
                                      catalog_type=catalog_type, catalog_connection_uri=catalog_connection_uri,
                                      catalog_version=catalog_version, catalog_table=catalog_table,
                                      catalog_database=catalog_database)

    def register_examples(self, example_meta_list: List[ExampleMeta]) -> List[ExampleMeta]:
        """
        register multiple examples in metadata store.

        :param example_meta_list: A list of examples
        :return: List of :py:class:`ai_flow.meta.example_meta.ExampleMeta` objects.
        """
        try:
            examples = MetaToTable.example_meta_list_to_table(example_meta_list, store_type=type(self).__name__)
            for ex in examples:
                ex.save()
            for example_meta, example in zip(example_meta_list, examples):
                example_meta.uuid = example.uuid
            return example_meta_list
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    def update_example(self, example_name: Text, support_type: ExampleSupportType, data_type: Text = None,
                       data_format: Text = None, description: Text = None, batch_uri: Text = None,
                       stream_uri: Text = None, update_time: int = None, properties: Properties = None,
                       name_list: List[Text] = None, type_list: List[DataType] = None, catalog_name: Text = None,
                       catalog_type: Text = None, catalog_database: Text = None,
                       catalog_connection_uri: Text = None, catalog_version: Text = None,
                       catalog_table: Text = None) -> Optional[ExampleMeta]:

        try:
            example: MongoExample = MongoExample.objects(name=example_name).first()
            if example is None:
                return None
            if support_type is not None:
                example.support_type = support_type
            if data_type is not None:
                example.data_type = data_type
            if data_format is not None:
                example.format = data_format
            if description is not None:
                example.description = description
            if batch_uri is not None:
                example.batch_uri = batch_uri
            if stream_uri is not None:
                example.stream_uri = stream_uri
            if update_time is not None:
                example.update_time = update_time
            if properties is not None:
                example.properties = str(properties)
            if name_list is not None:
                example.name_list = str(name_list)
            if type_list is not None:
                data_type_list = []
                for data_type in type_list:
                    data_type_list.append(data_type.value)
                data_type_list = str(data_type_list)
                example.type_list = data_type_list
            if catalog_name is not None:
                example.catalog_name = catalog_name
            if catalog_type is not None:
                example.catalog_type = catalog_type
            if catalog_database is not None:
                example.catalog_database = catalog_database
            if catalog_connection_uri is not None:
                example.connection_config = catalog_connection_uri
            if catalog_version is not None:
                example.catalog_version = catalog_version
            if catalog_table is not None:
                example.catalog_table = catalog_table
            example.save()
            return ResultToMeta.result_to_example_meta(example)
        except mongoengine.OperationError as e:
            raise AIFlowException(e)

    def list_example(self, page_size, offset) -> Optional[List[ExampleMeta]]:
        """
        List registered examples in metadata store.

        :param page_size: the limitation of the listed examples.
        :param offset: the offset of listed examples.
        :return: List of :py:class:`ai_flow.meta.example_meta.ExampleMeta` objects,
                 return None if no examples to be listed.
        """
        example_result = MongoExample.objects(is_deleted__ne=TRUE).skip(offset).limit(page_size)
        if len(example_result) == 0:
            return None
        example_list = []
        for example in example_result:
            example_list.append(ResultToMeta.result_to_example_meta(example))
        return example_list

    def delete_example_by_name(self, example_name) -> Status:
        """
        Delete the registered example by example name .

        :param example_name: the example name
        :return: Status.OK if the example is successfully deleted, Status.ERROR if the example does not exist otherwise.
        """
        try:
            example = MongoExample.objects(name=example_name, is_deleted__ne=TRUE).first()
            if example is None:
                return Status.ERROR
            deleted_example_counts = MongoExample.objects(
                name__startswith=deleted_character + example_name + deleted_character, is_deleted=TRUE).count()
            example.is_deleted = TRUE
            example.name = deleted_character + example.name + deleted_character + str(deleted_example_counts + 1)
            example.save()
            return Status.OK
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    def delete_example_by_id(self, example_id) -> Status:
        """
        Delete the registered example by example id .

        :param example_id: the example id
        :return: Status.OK if the example is successfully deleted, Status.ERROR if the example does not exist otherwise.
        """
        example = self.get_example_by_id(example_id=example_id)
        if example is None:
            return Status.ERROR
        return self.delete_example_by_name(example.name)

    """project api"""

    def get_project_by_id(self, project_id) -> Optional[ProjectMeta]:
        """
        get an specific project in metadata store by project id

        :param project_id: the project id
        :return: A single :py:class:`ai_flow.meta.project.ProjectMeta` object if the project exists,
                 Otherwise, returns None if the project does not exist.
        """
        project_result = MongoProject.objects(uuid=project_id, is_deleted__ne=TRUE)
        if len(project_result) == 0:
            return None
        return ResultToMeta.result_to_project_meta(project_result[0])

    def get_project_by_name(self, project_name) -> Optional[ProjectMeta]:
        """
        get an specific project in metadata store by project name
        :param project_name: the project name
        :return: A single :py:class:`ai_flow.meta.project.ProjectMeta` object if the project exists,
                 Otherwise, returns None if the project does not exist.
        """
        project_result = MongoProject.objects(name=project_name, is_deleted__ne=TRUE)
        if len(project_result) == 0:
            return None
        return ResultToMeta.result_to_project_meta(project_result[0])

    def register_project(self, name: Text, uri: Text, properties: Properties = None,
                         user: Text = None, password: Text = None, project_type: Text = None) -> ProjectMeta:
        """
        register a project in metadata store.

        :param name: the name of the project
        :param uri: the uri of the project
        :param properties: the properties of the project
        :param user: the user of the project
        :param password: the password of the project
        :param project_type: the project type of the project
        :return: A single :py:class:`ai_flow.meta.project.ProjectMeta` object.
        """
        before_project = self.get_project_by_name(project_name=name)
        if before_project is not None:
            # if the user has registered exactly the same project before,
            # do nothing in metadata store and return the registered project.
            if _compare_project_fields(uri, properties, user, password, project_type, before_project):
                return before_project
            else:
                # if the project registered this time has the same name but different fields,
                # raise the AIFlowException.
                raise AIFlowException("You have registered the project with same name: \"{}\" "
                                      "but different fields".format(name))

        try:
            project = MetaToTable.project_meta_to_table(name=name, uri=uri, properties=properties,
                                                        user=user, password=password,
                                                        project_type=project_type,
                                                        store_type=type(self).__name__)
            project.save()
            project_meta = ProjectMeta(uuid=project.uuid, name=name, uri=uri, properties=properties, user=user,
                                       password=password, project_type=project_type)
            return project_meta
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered Project (name={}) already exists. '
                                  'Error: {}'.format(project.name, str(e)))

    def list_project(self, page_size, offset) -> Optional[List[ProjectMeta]]:
        """
        List registered projects in metadata store.

        :param page_size: the limitation of the listed projects.
        :param offset: the offset of listed projects.
        :return: List of :py:class:`ai_flow.meta.project_meta.ProjectMeta` objects,
                 return None if no projects to be listed.
        """

        project_result = MongoProject.objects(is_deleted__ne=TRUE).skip(offset).limit(page_size)
        if len(project_result) == 0:
            return None
        projects = []
        for project in project_result:
            projects.append(ResultToMeta.result_to_project_meta(project))
        return projects

    def update_project(self, project_name: Text, uri: Text = None, properties: Properties = None,
                       user: Text = None, password: Text = None, project_type: Text = None) -> Optional[ProjectMeta]:
        try:
            project: MongoProject = MongoProject.objects(name=project_name, is_deleted__ne=TRUE).first()
            if project is None:
                return None
            if uri is not None:
                project.uri = uri
            if properties is not None:
                project.properties = str(properties)
            if user is not None:
                project.user = user
            if password is not None:
                project.password = password
            if project_type is not None:
                project.project_type = project_type
            project.save()
            return ResultToMeta.result_to_project_meta(project)
        except mongoengine.OperationError as e:
            raise AIFlowException(e)

    def delete_project_by_id(self, project_id) -> Status:
        """
        Delete the registered project by project id .

        :param project_id: the project id
        :return: Status.OK if the project is successfully deleted, Status.ERROR if the project does not exist otherwise.
        """
        project = self.get_project_by_id(project_id=project_id)
        if project is None:
            return Status.ERROR
        return self.delete_project_by_name(project_name=project.name)

    def delete_project_by_name(self, project_name) -> Status:
        """
        Delete the registered project by project name .

        :param project_name: the project name
        :return: Status.OK if the project is successfully deleted, Status.ERROR if the project does not exist otherwise.
        """
        try:
            project = MongoProject.objects(name=project_name, is_deleted__ne=TRUE).first()
            if project is None:
                return Status.ERROR
            deleted_project_counts = MongoProject.objects(
                name__startswith=deleted_character + project_name + deleted_character,
                is_deleted=TRUE).count()
            project.is_deleted = TRUE
            project.name = deleted_character + project.name + deleted_character + str(deleted_project_counts + 1)
            job_list = []
            model_version_list = []
            for per_workflow_execution in project.workflow_execution:
                deleted_workflow_execution_counts = MongoWorkflowExecution.objects(
                    name__startswith=deleted_character + per_workflow_execution.name + deleted_character,
                    is_deleted=TRUE).count()
                per_workflow_execution.is_deleted = TRUE
                per_workflow_execution.name = deleted_character + per_workflow_execution.name + deleted_character + str(
                    deleted_workflow_execution_counts + 1)
                for per_job in per_workflow_execution.job_info:
                    deleted_job_counts = MongoJob.objects(
                        name__startswith=deleted_character + per_job.name + deleted_character,
                        is_deleted=TRUE).count()
                    per_job.is_deleted = TRUE
                    per_job.name = deleted_character + per_job.name + deleted_character + str(
                        deleted_job_counts + 1)
                job_list += per_workflow_execution.job_info
            for per_model in project.model_relation:
                deleted_model_relation_counts = MongoModelRelation.objects(
                    name__startswith=deleted_character + per_model.name + deleted_character,
                    is_deleted=TRUE).count()
                per_model.is_deleted = TRUE
                per_model.name = deleted_character + per_model.name + deleted_character + str(
                    deleted_model_relation_counts + 1)
                for model_version in per_model.model_version_relation:
                    deleted_model_version_relation_counts = MongoModelVersionRelation.objects(
                        version__startswith=deleted_character + model_version.version + deleted_character,
                        is_deleted=TRUE).count()
                    model_version.is_deleted = TRUE
                    model_version.version = deleted_character + model_version.version + deleted_character + str(
                        deleted_model_version_relation_counts + 1)
                    # update for unqine constraint
                    model_version.version_model_id_unique = f'{model_version.version}-{model_version.model_id}'
                    model_version.version_workflow_execution_id_unique = f'{model_version.version}-{model_version.workflow_execution_id}'
                model_version_list += per_model.model_version_relation
            self._save_all(
                [project] + project.workflow_execution + project.model_relation + job_list + model_version_list)
            return Status.OK
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    """model api"""

    def get_model_relation_by_id(self, model_id) -> Optional[ModelRelationMeta]:
        """
        get an specific model relation in metadata store by model id.

        :param model_id: the model id
        :return: A single :py:class:`ai_flow.meta.model_relation_meta.ModelRelationMeta` object if the model relation
                 exists, Otherwise, returns None if the model relation does not exist.
        """
        model_result = MongoModelRelation.objects(uuid=model_id, is_deleted__ne=TRUE)
        if len(model_result) == 0:
            return None
        return ResultToMeta.result_to_model_relation_meta(model_result[0])

    def get_model_relation_by_name(self, name) -> Optional[ModelRelationMeta]:
        """
        get an specific model relation in metadata store by model name.

        :param name: the model name
        :return: A single :py:class:`ai_flow.meta.model_relation_meta.ModelRelationMeta` object if the model relation
                 exists, Otherwise, returns None if the model relation does not exist.
        """
        model_result = MongoModelRelation.objects(name=name, is_deleted__ne=TRUE)
        if len(model_result) == 0:
            return None
        return ResultToMeta.result_to_model_relation_meta(model_result[0])

    def register_model_relation(self, name: Text,
                                project_id: int = None) -> ModelRelationMeta:
        """
        register a model relation in metadata store

        :param name: the name of the model
        :param project_id: the project id which the model corresponded to.
        :return: A single :py:class:`ai_flow.meta.model_relation_meta.ModelRelationMeta` object.
        """
        before_model_relation = self.get_model_relation_by_name(name=name)
        if before_model_relation is not None:
            # if the user has registered exactly the same model relation before,
            # do nothing in metadata store and return the registered model relation.
            if _compare_model_relation_fields(project_id, before_model_relation):
                return before_model_relation
            else:
                # if the example registered this time has the same name but different fields,
                # raise the AIFlowException.
                raise AIFlowException("You have registered the model relation with same name: \"{}\" "
                                      "but different project uuid".format(name))
        try:
            model = MetaToTable.model_relation_meta_to_table(name=name,
                                                             project_id=project_id,
                                                             store_type=type(self).__name__)
            model.save()
            # update reference field
            if project_id is not None:
                project = MongoProject.objects(uuid=project_id).first()
                if project.model_relation is None:
                    project.model_relation = [model]
                else:
                    project.model_relation.append(model)
                project.save()
            model_meta = ModelRelationMeta(uuid=model.uuid, name=name, project_id=project_id)
            return model_meta
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered Model (name={}) already exists. '
                                  'Error: {}'.format(model.name, str(e)))

    def list_model_relation(self, page_size, offset) -> Optional[List[ModelRelationMeta]]:
        """
        List registered model relations in metadata store.

        :param page_size: the limitation of the listed model relations.
        :param offset: the offset of listed model relations.
        :return: List of :py:class:`ai_flow.meta.model_relation_meta.ModelRelationMeta` objects,
                 return None if no model relations to be listed.
        """
        model_result = MongoModelRelation.objects(is_deleted__ne=TRUE).skip(offset).limit(page_size)
        if len(model_result) == 0:
            return None
        models = []
        for model in model_result:
            models.append(ResultToMeta.result_to_model_relation_meta(model))
        return models

    def delete_model_relation_by_id(self, model_id) -> Status:
        """
        Delete the registered model by model id .

        :param model_id: the model id
        :return: Status.OK if the model is successfully deleted, Status.ERROR if the model does not exist otherwise.
        """
        model = self.get_model_relation_by_id(model_id=model_id)
        if model is None:
            return Status.ERROR
        return self.delete_model_relation_by_name(model_name=model.name)

    def delete_model_relation_by_name(self, model_name) -> Status:
        """
        Delete the registered model by model name .

        :param model_name: the model name
        :return: Status.OK if the model is successfully deleted, Status.ERROR if the model does not exist otherwise.
        """
        try:
            model = MongoModelRelation.objects(name=model_name, is_deleted__ne=TRUE).first()
            if model is None:
                return Status.ERROR
            deleted_model_counts = MongoModelRelation.objects(
                name__startswith=deleted_character + model.name + deleted_character,
                is_deleted=TRUE).count()
            model.is_deleted = TRUE
            model.name = deleted_character + model.name + deleted_character + str(deleted_model_counts + 1)
            for model_version in model.model_version_relation:
                deleted_model_version_counts = MongoModelVersionRelation.objects(
                    version__startswith=deleted_character + model_version.version + deleted_character,
                    is_deleted=TRUE).count()
                model_version.is_deleted = TRUE
                model_version.version = deleted_character + model_version.version + deleted_character + str(
                    deleted_model_version_counts + 1)
                # update for unqine constraint
                model_version.version_model_id_unique = f'{model_version.version}-{model_version.model_id}'
                model_version.version_workflow_execution_id_unique = f'{model_version.version}-{model_version.workflow_execution_id}'
            self._save_all([model] + model.model_version_relation)
            return Status.OK
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    '''workflow execution api'''

    def get_workflow_execution_by_id(self, execution_id) -> Optional[WorkflowExecutionMeta]:
        """
        get an specific workflow execution in metadata store by workflow execution id.

        :param execution_id: the workflow execution id
        :return: A single :py:class:`ai_flow.meta.workflow_execution_meta.WorkflowExecutionMeta` object
                 if the workflow execution exists, Otherwise, returns None if the workflow execution does not exist.
        """
        execution_result = MongoWorkflowExecution.objects(uuid=execution_id, is_deleted__ne=TRUE)
        if len(execution_result) == 0:
            return None
        workflow_execution = ResultToMeta.result_to_workflow_execution_meta(execution_result[0])
        return workflow_execution

    def get_workflow_execution_by_name(self, execution_name) -> Optional[WorkflowExecutionMeta]:
        """
        get an specific workflow execution in metadata store by workflow execution name.

        :param execution_name: the workflow execution name
        :return: A single :py:class:`ai_flow.meta.workflow_execution_meta.WorkflowExecutionMeta` object
                 if the workflow execution exists, Otherwise, returns None if the workflow execution does not exist.
        """
        execution_result = MongoWorkflowExecution.objects(name=execution_name, is_deleted__ne=TRUE)
        if len(execution_result) == 0:
            return None
        workflow_execution = ResultToMeta.result_to_workflow_execution_meta(execution_result[0])
        return workflow_execution

    def register_workflow_execution(self, name: Text,
                                    execution_state: State, project_id: int = None,
                                    properties: Properties = None, start_time: int = None,
                                    end_time: int = None, log_uri: Text = None,
                                    workflow_json=None, signature=None) -> WorkflowExecutionMeta:
        """
        register a workflow execution in metadata store.

        :param name: the name of the workflow execution
        :param execution_state: the execution state of the workflow execution
        :param project_id: the project id corresponded to the workflow execution
        :param properties: the properties of the workflow execution
        :param start_time: the time when the workflow execution started
        :param end_time: the time when the workflow execution ended
        :param log_uri: the log uri of the workflow execution
        :param workflow_json: the workflow json of the workflow execution
        :param signature: the signature of the workflow execution
        :return: A single :py:class:`ai_flow.meta.workflow_execution_meta.WorkflowExecutionMeta` object.
        """
        try:
            execution = MetaToTable.workflow_execution_meta_to_table(name=name,
                                                                     project_id=project_id,
                                                                     execution_state=execution_state,
                                                                     properties=properties, start_time=start_time,
                                                                     end_time=end_time,
                                                                     log_uri=log_uri, workflow_json=workflow_json,
                                                                     signature=signature,
                                                                     store_type=type(self).__name__)
            execution.save()
            # update reference field
            if project_id is not None:
                project = MongoProject.objects(uuid=project_id).first()
                if project.workflow_execution is None:
                    project.workflow_execution = [execution]
                else:
                    project.workflow_execution.append(execution)
                project.save()
            execution_meta = WorkflowExecutionMeta(uuid=execution.uuid, name=name, project_id=project_id,
                                                   execution_state=execution_state,
                                                   properties=properties, start_time=start_time,
                                                   end_time=end_time, log_uri=log_uri, workflow_json=workflow_json,
                                                   signature=signature)
            return execution_meta
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered WorkflowExecution (name={}) already exists. '
                                  'Error: {}'.format(execution.name, str(e)))

    def list_workflow_execution(self, page_size, offset) -> Optional[List[WorkflowExecutionMeta]]:
        """
        List registered workflow executions in metadata store.

        :param page_size: the limitation of the listed workflow executions.
        :param offset: the offset of listed workflow executions.
        :return: List of :py:class:`ai_flow.meta.workflow_execution_meta.WorkflowExecutionMeta` object,
                 return None if no workflow executions to be listed.
        """
        execution_result = MongoWorkflowExecution.objects(is_deleted__ne=TRUE).skip(offset).limit(page_size)
        if len(execution_result) == 0:
            return None
        workflow_execution_list = []
        for execution in execution_result:
            workflow_execution_list.append(ResultToMeta.result_to_workflow_execution_meta(execution))
        return workflow_execution_list

    def update_workflow_execution(self, execution_name: Text,
                                  execution_state: State = None, project_id: int = None,
                                  properties: Properties = None, end_time: int = None,
                                  log_uri: Text = None, workflow_json: Text = None, signature: Text = None) -> \
            Optional[WorkflowExecutionMeta]:
        try:
            workflow_execution: MongoWorkflowExecution = MongoWorkflowExecution.objects(name=execution_name,
                                                                                        is_deleted__ne=TRUE).first()
            if workflow_execution is None:
                return None
            if execution_state is not None:
                workflow_execution.execution_state = execution_state
            if project_id is not None:
                project = self.get_project_by_id(project_id)
                if project is None:
                    raise AIFlowException(
                        'The project related to the project id={} does not exist'.format(project_id))
                workflow_execution.project_id = project_id
            if properties is not None:
                workflow_execution.properties = str(properties)
            if end_time is not None:
                workflow_execution.end_time = end_time
            if log_uri is not None:
                workflow_execution.log_uri = log_uri
            if workflow_json is not None:
                workflow_execution.workflow_json = workflow_json
            if signature is not None:
                workflow_execution.signature = signature
            workflow_execution.save()
            return ResultToMeta.result_to_workflow_execution_meta(workflow_execution)
        except mongoengine.OperationError as e:
            raise AIFlowException(e)

    def update_workflow_execution_end_time(self, end_time, execution_name):
        """
        update the workflow execution end time in metadata store.

        :param end_time: the time when the workflow execution ended.
        :param execution_name: the execution name
        :return: the workflow execution uuid if the workflow execution is successfully updated, raise an exception
                 if fail to update otherwise.
        """
        try:
            workflow_execution = MongoWorkflowExecution.objects(name=execution_name).first()
            if workflow_execution is None:
                return UPDATE_FAIL
            workflow_execution.end_time = end_time
            workflow_execution.save()
            return workflow_execution.uuid
        except mongoengine.OperationError as e:
            raise AIFlowException(e)

    def update_workflow_execution_state(self, state: State, execution_name):
        """
        update the workflow execution end time in metadata store.

        :param state: the state of the workflow execution.
        :param execution_name: the execution name
        :return: the workflow execution uuid if the workflow execution is successfully updated, raise an exception
                 if fail to update otherwise.
        """
        try:
            workflow_execution = MongoWorkflowExecution.objects(name=execution_name).first()
            if workflow_execution is None:
                return UPDATE_FAIL
            workflow_execution.execution_state = state
            workflow_execution.save()
            return workflow_execution.uuid
        except mongoengine.OperationError as e:
            raise AIFlowException(e)

    def delete_workflow_execution_by_id(self, execution_id) -> Status:
        """
        Delete the registered workflow execution by workflow execution id .

        :param execution_id: the workflow execution id
        :return: Status.OK if the workflow execution is successfully deleted,
                 Status.ERROR if the workflow execution does not exist otherwise.
        """
        execution = self.get_workflow_execution_by_id(execution_id=execution_id)
        if execution is None:
            return Status.ERROR
        return self.delete_workflow_execution_by_name(execution_name=execution.name)

    def delete_workflow_execution_by_name(self, execution_name) -> Status:
        """
        Delete the registered workflow execution by workflow execution name .

        :param execution_name: the workflow execution name
        :return: Status.OK if the workflow execution is successfully deleted,
                 Status.ERROR if the workflow execution does not exist otherwise.
        """
        try:
            execution = MongoWorkflowExecution.objects(name=execution_name, is_deleted__ne=TRUE).first()
            if execution is None:
                return Status.ERROR
            deleted_execution_counts = MongoWorkflowExecution.objects(
                name__startswith=deleted_character + execution.name + deleted_character,
                is_deleted=TRUE).count()
            execution.is_deleted = TRUE
            execution.name = deleted_character + execution.name + deleted_character + str(
                deleted_execution_counts + 1)
            for per_job in execution.job_info:
                deleted_job_counts = MongoJob.objects(
                    name__startswith=deleted_character + per_job.name + deleted_character,
                    is_deleted=TRUE).count()
                per_job.is_deleted = TRUE
                per_job.name = deleted_character + per_job.name + deleted_character + str(deleted_job_counts + 1)
            for model_version in execution.model_version_relation:
                deleted_model_version_counts = MongoModelVersionRelation.objects(
                    version__startswith=deleted_character + model_version.version + deleted_character,
                    is_deleted=TRUE).count()
                model_version.is_deleted = TRUE
                model_version.version = deleted_character + model_version.version + deleted_character + str(
                    deleted_model_version_counts + 1)
                # update for unqine constraint
                model_version.version_model_id_unique = f'{model_version.version}-{model_version.model_id}'
                model_version.version_workflow_execution_id_unique = f'{model_version.version}-{model_version.workflow_execution_id}'
            self._save_all([execution] + execution.job_info + execution.model_version_relation)
            return Status.OK
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    '''model version api'''

    def get_model_version_relation_by_version(self, version_name, model_id) -> Optional[ModelVersionRelationMeta]:
        """
        get an specific model version relation in metadata store by the model version name.

        :param version_name: the model version name
        :param model_id: the model id corresponded to the model version
        :return: A single :py:class:`ai_flow.meta.model_relation_meta.ModelVersionRelationMeta` object
                 if the model version exists, Otherwise, returns None if the model version does not exist.
        """
        model_version_result = MongoModelVersionRelation.objects(version=version_name, model_id=model_id,
                                                                 is_deleted__ne=TRUE)
        if len(model_version_result) == 0:
            return None
        return ResultToMeta.result_to_model_version_relation_meta(model_version_result[0])

    def register_model_version_relation(self, version: Text,
                                        model_id: int,
                                        workflow_execution_id: int = None) -> ModelVersionRelationMeta:
        """
        register a model version relation in metadata store.

        :param version: the specific model version
        :param model_id: the model id corresponded to the model version
        :param workflow_execution_id: the workflow execution id corresponded to the model version
        :return: A single :py:class:`ai_flow.meta.model_relation_meta.ModelVersionRelationMeta` object.
        """
        try:
            model_version_relation = MetaToTable.model_version_relation_to_table(version=version,
                                                                                 model_id=model_id,
                                                                                 workflow_execution_id=workflow_execution_id,
                                                                                 store_type=type(self).__name__)
            model_version_relation.save()
            # update reference field
            if model_id is not None:
                model_relation = MongoModelRelation.objects(uuid=model_id).first()
                if model_relation.model_version_relation is None:
                    model_relation.model_version_relation = [model_version_relation]
                else:
                    model_relation.model_version_relation.append(model_version_relation)
                model_relation.save()

            if workflow_execution_id is not None:
                workflow_execution = MongoWorkflowExecution.objects(uuid=workflow_execution_id).first()
                if workflow_execution.model_version_relation is None:
                    workflow_execution.model_version_relation = [model_version_relation]
                else:
                    workflow_execution.model_version_relation.append(model_version_relation)
                workflow_execution.save()

            model_version_relation_meta = ModelVersionRelationMeta(version=version, model_id=model_id,
                                                                   workflow_execution_id=workflow_execution_id)
            return model_version_relation_meta
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered ModelVersion (name={}) already exists. '
                                  'Error: {}'.format(model_version_relation.version, str(e)))

    def list_model_version_relation(self, model_id, page_size, offset) -> Optional[List[ModelVersionRelationMeta]]:
        """
        List registered model version relations in metadata store.

        :param model_id: the model id corresponded to the model version
        :param page_size: the limitation of the listed model version relations.
        :param offset: the offset of listed model version relations.
        :return: List of :py:class:`ai_flow.meta.model_relation_meta.ModelRelationMeta` objects,
                 return None if no model version relations to be listed.
        """
        model_version_result = MongoModelVersionRelation.objects(model_id=model_id, is_deleted__ne=TRUE).skip(
            offset).limit(page_size)
        if len(model_version_result) == 0:
            return None
        model_version_list = []
        for version in model_version_result:
            model_version_list.append(ResultToMeta.result_to_model_version_relation_meta(version))
        return model_version_list

    def delete_model_version_relation_by_version(self, version, model_id) -> Status:
        """
        Delete the registered model version by model version name .

        :param version: the model version name
        :param model_id: the model id corresponded to the model version
        :return: Status.OK if the model version is successfully deleted,
                 Status.ERROR if the model version does not exist otherwise.
        """
        try:
            model_version = MongoModelVersionRelation.objects(version=version, model_id=model_id,
                                                              is_deleted__ne=TRUE).first()
            if model_version is None:
                return Status.ERROR
            deleted_model_version_counts = MongoModelVersionRelation.objects(
                version__startswith=deleted_character + version + deleted_character,
                is_deleted=TRUE).count()
            model_version.is_deleted = TRUE
            model_version.version = deleted_character + model_version.version + deleted_character + str(
                deleted_model_version_counts + 1)
            # update for unqine constraint
            model_version.version_model_id_unique = f'{model_version.version}-{model_version.model_id}'
            model_version.version_workflow_execution_id_unique = f'{model_version.version}-{model_version.workflow_execution_id}'
            model_version.save()
            return Status.OK
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    """job api"""

    def get_job_by_id(self, job_id) -> Optional[JobMeta]:
        """
        get an specific job in metadata store by job id.

        :param job_id: the job id
        :return: A single :py:class:`ai_flow.meta.job_meta.JobMeta` object
                 if the job exists, Otherwise, returns None if the job does not exist.
        """
        job_result = MongoJob.objects(uuid=job_id, is_deleted__ne=TRUE)
        if len(job_result) == 0:
            return None
        job = ResultToMeta.result_to_job_meta(job_result[0])
        return job

    def get_job_by_name(self, job_name) -> Optional[JobMeta]:
        """
        get an specific job in metadata store by job name.

        :param job_name: the job name
        :return: A single :py:class:`ai_flow.meta.job_meta.JobMeta` object
                 if the job exists, Otherwise, returns None if the job does not exist.
        """
        job_result = MongoJob.objects(name=job_name, is_deleted__ne=TRUE)
        if len(job_result) == 0:
            return None
        job = ResultToMeta.result_to_job_meta(job_result[0])
        return job

    def register_job(self, name: Text, job_state: State, workflow_execution_id: int = None,
                     properties: Properties = None, job_id: Text = None,
                     start_time: int = None, end_time: int = None,
                     log_uri: Text = None, signature: Text = None) -> JobMeta:
        """
        register a job in metadata store.

        :param name: the name of the job
        :param job_state: the state of the job
        :param workflow_execution_id: the workflow execution id corresponded to the job
        :param properties: the properties of the job
        :param job_id: the job_id of the job
        :param start_time: the time when the job started
        :param end_time: the time when the job ended
        :param log_uri: the log uri of the job
        :param signature: the signature of the job
        :return: A single :py:class:`ai_flow.meta.job_meta.JobMeta` object.
        """
        try:
            job = MetaToTable.job_meta_to_table(name=name,
                                                job_state=job_state, properties=properties,
                                                job_id=job_id, workflow_execution_id=workflow_execution_id,
                                                start_time=start_time, end_time=end_time,
                                                log_uri=log_uri,
                                                signature=signature,
                                                store_type=type(self).__name__)
            job.save()
            # update reference field
            if workflow_execution_id is not None:
                workflow_execution = MongoWorkflowExecution.objects(uuid=workflow_execution_id).first()
                if workflow_execution.job_info is None:
                    workflow_execution.job_info = [job]
                else:
                    workflow_execution.job_info.append(job)
                workflow_execution.save()
            job_meta = JobMeta(uuid=job.uuid, name=name, workflow_execution_id=workflow_execution_id,
                               job_state=job_state,
                               properties=properties, job_id=job_id, start_time=start_time, end_time=end_time,
                               log_uri=log_uri, signature=signature)
            return job_meta
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered Job (name={}) already exists. '
                                  'Error: {}'.format(job.name, str(e)))

    def update_job(self, job_name: Text, job_state: State = None, properties: Properties = None,
                   job_id: Text = None, workflow_execution_id: int = None,
                   end_time: int = None, log_uri: Text = None, signature: Text = None) -> Optional[JobMeta]:
        try:
            job: MongoJob = MongoJob.objects(name=job_name, is_deleted__ne=TRUE).first()
            if job is None:
                return None
            if job_state is not None:
                job.job_state = job_state
            if properties is not None:
                job.properties = str(properties)
            if job_id is not None:
                job.job_id = job_id
            if workflow_execution_id is not None:
                workflow_execution = self.get_workflow_execution_by_id(workflow_execution_id)
                if workflow_execution is None:
                    raise AIFlowException('The workflow execution related to the workflow execution id={} '
                                          'does not exist'.format(workflow_execution_id))
                job.workflow_execution_id = workflow_execution_id
            if end_time is not None:
                job.end_time = end_time
            if log_uri is not None:
                job.log_uri = log_uri
            if signature is not None:
                job.signature = signature
            job.save()
            return ResultToMeta.result_to_job_meta(job)
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    def list_job(self, page_size, offset) -> Optional[List[JobMeta]]:
        """
        List registered jobs in metadata store.

        :param page_size: the limitation of the listed jobs.
        :param offset: the offset of listed jobs.
        :return: List of :py:class:`ai_flow.meta.job_meta.JobMeta` objects,
                 return None if no jobs to be listed.
        """
        job_result = MongoJob.objects(is_deleted__ne=TRUE).skip(offset).limit(page_size)
        if len(job_result) == 0:
            return None
        job_list = []
        for job in job_result:
            job_list.append(ResultToMeta.result_to_job_meta(job))
        return job_list

    def update_job_state(self, job_state: State, job_name):
        """
        update the job state in metadata store.

        :param job_state: the state of the job.
        :param job_name: the job name
        :return: the job uuid if the job is successfully updated, raise an exception if fail to update otherwise.
        """
        try:
            job_update = MongoJob.objects(name=job_name).first()
            if job_update is None:
                return UPDATE_FAIL
            job_update.job_state = job_state
            job_update.save()
            return job_update.uuid
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    def update_job_end_time(self, end_time, job_name):
        """
        update the job end time in metadata store.

        :param end_time: the time when the job ended.
        :param job_name: the job name
        :return: the job uuid if the job is successfully updated, raise an exception if fail to update otherwise.
        """
        try:
            job_update = MongoJob.objects(name=job_name).first()
            if job_update is None:
                return UPDATE_FAIL
            job_update.end_time = end_time
            job_update.save()
            return job_update.uuid
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    def delete_job_by_id(self, job_id) -> Status:
        """
        Delete the registered job by job id .

        :param job_id: the job id
        :return: Status.OK if the job is successfully deleted,
                 Status.ERROR if the job does not exist otherwise.
        """
        job = self.get_job_by_id(job_id=job_id)
        if job is None:
            return Status.ERROR
        return self.delete_job_by_name(job.name)

    def delete_job_by_name(self, job_name) -> Status:
        """
        Delete the registered job by job name .

        :param job_name: the job name
        :return: Status.OK if the job is successfully deleted,
                 Status.ERROR if the job does not exist otherwise.
        """
        try:
            job = MongoJob.objects(name=job_name, is_deleted__ne=TRUE).first()
            if job is None:
                return Status.ERROR
            deleted_job_counts = MongoJob.objects(
                name__startswith=deleted_character + job_name + deleted_character,
                is_deleted=TRUE).count()
            job.is_deleted = TRUE
            job.name = deleted_character + job.name + deleted_character + str(deleted_job_counts + 1)
            job.save()
            return Status.OK
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    """artifact api"""

    def get_artifact_by_id(self, artifact_id: int) -> Optional[ArtifactMeta]:
        """
        get an specific artifact in metadata store by artifact id.

        :param artifact_id: the artifact id
        :return: A single :py:class:`ai_flow.meta.artifact_meta.ArtifactMeta` object
                 if the artifact exists, Otherwise, returns None if the artifact does not exist.
        """
        artifact_result = MongoArtifact.objects(uuid=artifact_id, is_deleted__ne=TRUE)
        if len(artifact_result) == 0:
            return None
        artifact = ResultToMeta.result_to_artifact_meta(artifact_result[0])
        return artifact

    def get_artifact_by_name(self, artifact_name: Text) -> Optional[ArtifactMeta]:
        """
        get an specific artifact in metadata store by artifact name.

        :param artifact_name: the artifact name
        :return: A single :py:class:`ai_flow.meta.artifact_meta.ArtifactMeta` object
                 if the artifact exists, Otherwise, returns None if the artifact does not exist.
        """
        artifact_result = MongoArtifact.objects(name=artifact_name, is_deleted__ne=TRUE)
        if len(artifact_result) == 0:
            return None
        artifact = ResultToMeta.result_to_artifact_meta(artifact_result[0])
        return artifact

    def register_artifact(self, name: Text, data_format: Text = None, description: Text = None,
                          batch_uri: Text = None, stream_uri: Text = None,
                          create_time: int = None, update_time: int = None,
                          properties: Properties = None) -> ArtifactMeta:
        """
        register an artifact in metadata store.

        :param name: the name of the artifact
        :param data_format: the data_format of the artifact
        :param description: the description of the artifact
        :param batch_uri: the batch uri of the artifact
        :param stream_uri: the stream uri of the artifact
        :param create_time: the time when the artifact is created
        :param update_time: the time when the artifact is updated
        :param properties: the properties of the artifact
        :return: A single :py:class:`ai_flow.meta.artifact_meta.py.ArtifactMeta` object.
        """
        before_artifact = self.get_artifact_by_name(artifact_name=name)
        if before_artifact is not None:
            # if the user has registered exactly the same artifact before,
            # do nothing in metadata store and return the registered artifact.
            if _compare_artifact_fields(data_format, description, batch_uri, stream_uri,
                                        create_time, update_time, properties, before_artifact):
                return before_artifact
            else:
                # if the artifact registered this time has the same name but different fields,
                # raise the AIFlowException.
                raise AIFlowException("You have registered the artifact with same name: \"{}\""
                                      " but different fields".format(name))
        try:
            artifact = MetaToTable.artifact_meta_to_table(name=name, data_format=data_format,
                                                          description=description,
                                                          batch_uri=batch_uri, stream_uri=stream_uri,
                                                          create_time=create_time,
                                                          update_time=update_time, properties=properties,
                                                          store_type=type(self).__name__)
            artifact.save()
            artifact_meta = ArtifactMeta(uuid=artifact.uuid, name=name, data_format=data_format,
                                         description=description,
                                         batch_uri=batch_uri, stream_uri=stream_uri,
                                         create_time=create_time,
                                         update_time=update_time, properties=properties)
            return artifact_meta
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered Artifact (name={}) already exists. '
                                  'Error: {}'.format(artifact.name, str(e)))

    def update_artifact(self, artifact_name: Text, data_format: Text = None, description: Text = None,
                        batch_uri: Text = None, stream_uri: Text = None,
                        update_time: int = None, properties: Properties = None) -> Optional[ArtifactMeta]:
        try:
            artifact: MongoArtifact = MongoArtifact.objects(name=artifact_name, is_deleted__ne=TRUE).first()
            if artifact is None:
                return None
            if data_format is not None:
                artifact.data_format = data_format
            if description is not None:
                artifact.description = description
            if batch_uri is not None:
                artifact.batch_uri = batch_uri
            if stream_uri is not None:
                artifact.stream_uri = stream_uri
            if update_time is not None:
                artifact.update_time = update_time
            if properties is not None:
                artifact.properties = str(properties)
            artifact.save()
            return ResultToMeta.result_to_artifact_meta(artifact)
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    def list_artifact(self, page_size, offset) -> Optional[List[ArtifactMeta]]:
        """
        List registered artifacts in metadata store.

        :param page_size: the limitation of the listed artifacts.
        :param offset: the offset of listed artifacts.
        :return: List of :py:class:`ai_flow.meta.artifact_meta.py.ArtifactMeta` objects,
                 return None if no artifacts to be listed.
        """
        artifact_result = MongoArtifact.objects(is_deleted__ne=TRUE).skip(offset).limit(page_size)
        if len(artifact_result) == 0:
            return None
        artifact_list = []
        for artifact in artifact_result:
            artifact_list.append(ResultToMeta.result_to_artifact_meta(artifact))
        return artifact_list

    def delete_artifact_by_id(self, artifact_id) -> Status:
        """
        Delete the registered artifact by artifact id .

        :param artifact_id: the artifact id
        :return: Status.OK if the artifact is successfully deleted,
                 Status.ERROR if the artifact does not exist otherwise.
        """
        artifact = self.get_artifact_by_id(artifact_id=artifact_id)
        if artifact is None:
            return Status.ERROR
        return self.delete_artifact_by_name(artifact.name)

    def delete_artifact_by_name(self, artifact_name) -> Status:
        """
        Delete the registered artifact by artifact name .

        :param artifact_name: the artifact name
        :return: Status.OK if the artifact is successfully deleted,
                 Status.ERROR if the artifact does not exist otherwise.
        """
        try:
            artifact = MongoArtifact.objects(name=artifact_name, is_deleted__ne=TRUE).first()
            if artifact is None:
                return Status.ERROR
            deleted_artifact_counts = MongoArtifact.objects(
                name__startswith=deleted_character + artifact_name + deleted_character,
                is_deleted=TRUE).count()
            artifact.is_deleted = TRUE
            artifact.name = deleted_character + artifact.name + deleted_character + str(deleted_artifact_counts + 1)
            artifact.save()
            return Status.OK
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    def get_deployed_model_version(self, model_name):
        return self.get_model_with_stage(model_name, STAGE_DEPLOYED)

    def get_latest_validated_model_version(self, model_name):
        return self.get_model_with_stage(model_name, STAGE_VALIDATED)

    def get_latest_generated_model_version(self, model_name):
        return self.get_model_with_stage(model_name, STAGE_GENERATED)

    def get_model_with_stage(self, model_name, stage):
        if model_name is None:
            raise AIFlowException('Registered model name cannot be empty.', INVALID_PARAMETER_VALUE)
        model_version = MongoModelVersion.objects(model_name=model_name, current_stage=stage)
        if len(model_version) == 0:
            return None
        else:
            return model_version[len(model_version) - 1].to_meta_entity()

    @classmethod
    def _get_registered_model(cls, model_name):
        """
        Query registered model in Model Center filter by model name

        :param model_name: Unique name for registered model within Model Center.
        """
        if model_name is None:
            raise AIFlowException('Registered model name cannot be empty.', INVALID_PARAMETER_VALUE)

        register_models = MongoRegisteredModel.objects(model_name=model_name)

        if len(register_models) == 0:
            return None
        else:
            _logger.info("Get registered model name: %s, versions: %s.", register_models[0].model_name,
                         register_models[0].model_version)
            return register_models[0]

    def create_registered_model(self, model_name, model_type=None, model_desc=None):
        """
        Create a new registered model in model repository.

        :param model_name: Name of registered model. This is expected to be unique in the backend store.
        :param model_type: (Optional) Type of registered model.
        :param model_desc: (Optional) Description of registered model.

        :return: Object of :py:class:`ai_flow.model_center.entity.RegisteredModel` created in Model Center.
        """
        if model_name is None:
            raise AIFlowException('Registered model name cannot be empty.', INVALID_PARAMETER_VALUE)
        try:
            before_model = self._get_registered_model(model_name=model_name)
            if before_model is not None:
                if _compare_model_fields(model_type, model_desc, before_model):
                    registered_model = MongoRegisteredModel(model_name=model_name,
                                                            model_type=model_type,
                                                            model_desc=model_desc)
                    return registered_model.to_meta_entity()
                else:
                    raise AIFlowException("You have registered the model with same name: \"{}\" "
                                          "but different fields".format(model_name), RESOURCE_ALREADY_EXISTS)
            registered_model = MongoRegisteredModel(model_name=model_name,
                                                    model_type=model_type,
                                                    model_desc=model_desc)
            registered_model.save()
            return registered_model.to_meta_entity()
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered Model (name={}) already exists. Error: {}'.format(model_name, str(e)),
                                  RESOURCE_ALREADY_EXISTS)

    def update_registered_model(self, registered_model, model_name=None, model_type=None, model_desc=None):
        """
        Update metadata for RegisteredModel entity. Either ``model_name`` or ``model_type`` or ``model_desc``
        should be non-None. Backend raises exception if registered model with given name does not exist.

        :param registered_model: :py:class:`ai_flow.model_center.entity.RegisteredModel` object.
        :param model_name: (Optional) New proposed name for the registered model.
        :param model_type: (Optional) Type of registered model.
        :param model_desc: (Optional) Description of registered model.

        :return: A single updated :py:class:`ai_flow.model_center.entity.RegisteredModel` object.
        """
        registered_model = self._get_registered_model(registered_model.model_name)
        if registered_model is None:
            return None
        else:
            try:
                if model_name is not None:
                    registered_model.model_name = model_name
                    # Update model name of registered model version
                    for model_version in registered_model.model_version:
                        model_version.model_name = model_name
                if model_type is not None:
                    registered_model.model_type = model_type
                if model_desc is not None:
                    registered_model.model_desc = model_desc
                self._save_all([registered_model] + registered_model.model_version)
                return registered_model.to_meta_entity()
            except mongoengine.OperationError as e:
                raise AIFlowException(
                    'Registered model (name={}) already exists. Error: {}'.format(model_name, str(e)),
                    RESOURCE_ALREADY_EXISTS)

    def delete_registered_model(self, registered_model):
        """
        Delete registered model based on specific model name.
        Backend raises exception if registered model with given name does not exist.

        :param registered_model: :py:class:`ai_flow.model_center.entity.RegisteredModel` object.

        :return: None
        """
        try:
            registered_model = self._get_registered_model(registered_model.model_name)
            registered_model.delete()
            return Status.OK
        except mongoengine.OperationError as e:
            raise AIFlowException(str(e))

    def list_registered_models(self):
        """
        List of registered models backend in Model Center.

        :return: List of :py:class:`ai_flow.model_center.entity.RegisteredModel` objects.
        """
        return [registered_model.to_detail_entity() for registered_model in MongoRegisteredModel.objects()]

    def get_registered_model_detail(self, registered_model):
        """
        Get registered model detail filter by model name and model version for Model Center.

        :param registered_model: :py:class:`ai_flow.model_center.entity.RegisteredModel` object.

        :return: Object of :py:class:`ai_flow.model_center.entity.RegisteredModelDetail` backend in Model Center.
        """
        registered_model = self._get_registered_model(registered_model.model_name)
        return None if registered_model is None else registered_model.to_detail_entity()

    @classmethod
    def _get_model_version(cls, model_version):
        model_name = model_version.model_name
        model_version = model_version.model_version
        if model_name is None:
            raise AIFlowException('Registered model name cannot be empty.', INVALID_PARAMETER_VALUE)
        if model_version is None:
            raise AIFlowException('Registered model version cannot be empty.', INVALID_PARAMETER_VALUE)

        model_versions = MongoModelVersion.objects(model_name=model_name, model_version=model_version,
                                                   current_stage__ne=STAGE_DELETED)

        if len(model_versions) == 0:
            return None
        else:
            _logger.info("Get registered model version: %s of model name: %s.", model_versions[0],
                         model_versions[0].model_name)
            return model_versions[0]

    @classmethod
    def _list_model_versions(cls, registered_model):
        model_name = registered_model.model_name
        if model_name is None:
            raise AIFlowException('Registered model name cannot be empty.', INVALID_PARAMETER_VALUE)

        return MongoModelVersion.objects(model_name=model_name, current_stage__ne=STAGE_DELETED)

    @classmethod
    def _count_deleted_model_version(cls, model_version):
        model_name = model_version.model_name
        model_version = model_version.model_version
        if model_name is None:
            raise AIFlowException('Registered model name cannot be empty.', INVALID_PARAMETER_VALUE)
        if model_version is None:
            raise AIFlowException('Registered model version cannot be empty.', INVALID_PARAMETER_VALUE)

        return MongoModelVersion.objects(model_name=model_name,
                                         model_version__startswith=deleted_character + model_version + deleted_character,
                                         current_stage__ne=STAGE_DELETED).count()

    def create_model_version(self, model_name, model_path, model_metric, model_flavor=None,
                             version_desc=None, current_stage=STAGE_GENERATED):
        """
        Create a new model version from given model source and model metric.

        :param model_name: Name for containing registered model.
        :param model_path: Source path where the AIFlow model is stored.
        :param model_metric: Metric address from AIFlow metric server of registered model.
        :param model_flavor: (Optional) Flavor feature of AIFlow registered model option.
        :param version_desc: (Optional) Description of registered model version.
        :param current_stage: (Optional) Stage of registered model version

        :return: Object of :py:class:`ai_flow.model_center.entity.ModelVersion` created in Model Center.
        """

        def next_version(current_version):
            if current_version is None:
                return "1"
            else:
                return str(current_version + 1)

        for attempt in range(self.CREATE_RETRY_TIMES):
            try:
                registered_model = self._get_registered_model(model_name)
                if registered_model is None:
                    return None
                else:
                    model_versions = self._list_model_versions(registered_model)
                    if model_versions is None:
                        version_num = 0
                    else:
                        version_num = len(model_versions)
                    model_version = next_version(version_num)
                    doc_model_version = MongoModelVersion(model_name=model_name,
                                                          model_version=model_version,
                                                          model_path=model_path,
                                                          model_metric=model_metric,
                                                          model_flavor=model_flavor,
                                                          version_desc=version_desc,
                                                          current_stage=get_canonical_stage(current_stage))
                    doc_model_version.save()
                    # update reference field
                    if model_name is not None:
                        registered_model = MongoRegisteredModel.objects(model_name=model_name).first()
                        if registered_model.model_version is None:
                            registered_model.model_version = [doc_model_version]
                        else:
                            registered_model.model_version.append(doc_model_version)
                        registered_model.save()
                    return doc_model_version.to_meta_entity()
            except mongoengine.OperationError as e:
                logging.info(model_version)
                more_retries = self.CREATE_RETRY_TIMES - attempt - 1
                _logger.info(
                    'Create model version (model_version=%s) error (model_name=%s). Retrying %s more time%s.',
                    model_version, model_name,
                    str(more_retries), 's' if more_retries > 1 else '')
        raise AIFlowException(
            'Create model version error (model_name={}). Giving up after {} attempts.'.format(model_name,
                                                                                              self.CREATE_RETRY_TIMES))

    def update_model_version(self, model_version, model_path=None, model_metric=None, model_flavor=None,
                             version_desc=None, current_stage=None):
        """
        Update metadata associated with a model version in model repository.

        :param model_version: :py:class:`ai_flow.model_center.entity.ModelVersion` object.
        :param model_path: (Optional) New Source path where AIFlow model is stored.
        :param model_metric: (Optional) New Metric address AIFlow metric server of registered model provided.
        :param model_flavor: (Optional) Flavor feature of AIFlow registered model option.
        :param version_desc: (Optional) New Description of registered model version.
        :param current_stage: (Optional) New desired stage for this model version.

        :return: A single updated :py:class:`ai_flow.model_center.entity.ModelVersion` object.
        """
        serving_model_version = self.get_deployed_model_version(model_version.model_name)
        if serving_model_version is not None and current_stage == 'DEPLOYED':
            raise AIFlowException('There is already a serving model version="{}" of model="{}"'.
                                  format(serving_model_version.model_version, serving_model_version.model_name))
        model_version = self._get_model_version(model_version)
        if model_version is None:
            return None
        else:
            try:
                if model_path is not None:
                    model_version.model_path = model_path
                if model_metric is not None:
                    model_version.model_metric = model_metric
                if model_flavor is not None:
                    model_version.model_flavor = model_flavor
                if version_desc is not None:
                    model_version.version_desc = version_desc
                if current_stage is not None:
                    model_version.current_stage = get_canonical_stage(current_stage)
                model_version.save()
                return model_version.to_meta_entity()
            except mongoengine.OperationError as e:
                raise AIFlowException(
                    'Update model version error (model_name={}, model_version={}).'.format(model_version.model_name,
                                                                                           model_version.model_version))

    def delete_model_version(self, model_version):
        """
        Delete model version in model repository.

        :param model_version: :py:class:`ai_flow.model_center.entity.ModelVersion` object.

        :return: None
        """
        doc_model_version = self._get_model_version(model_version)
        if doc_model_version is None:
            return None

        doc_model_version.model_path = "REDACTED-SOURCE-PATH"
        doc_model_version.model_metric = "REDACTED-METRIC-ADDRESS"
        doc_model_version.model_flavor = "REDACTED-FLAVOR-FEATURE"
        doc_model_version.version_status = None
        doc_model_version.version_desc = None
        doc_model_version.current_stage = STAGE_DELETED
        doc_model_version.save()

    def get_model_version_detail(self, model_version):
        """
        :param model_version: :py:class:`ai_flow.model_center.entity.ModelVersion` object.

        :return: Object of :py:class:`ai_flow.model_center.entity.ModelVersionDetail` backend in Model Center.
        """
        doc_model_version = self._get_model_version(model_version)
        return None if doc_model_version is None else doc_model_version.to_meta_entity()

    def register_metric_meta(self,
                             name,
                             dataset_id,
                             model_name,
                             model_version,
                             job_id,
                             start_time,
                             end_time,
                             metric_type,
                             uri,
                             tags,
                             metric_description,
                             properties) -> MetricMeta:
        """
        register metric meta to store
        :param name: the metric name
        :param dataset_id: the dataset id of the metric or model metric associate with dataset id
        :param model_name: if then model metric, associate with model name
        :param model_version: if then model metric, associate with model version
        :param job_id: the job_id which create the metric
        :param start_time:
        :param end_time:
        :param metric_type: MetricType DATASET or MODEL
        :param uri: the metric uri
        :param tags: such as flink,tensorflow
        :param metric_description:
        :param properties:
        :return:
        """
        try:
            metric_meta_table = metric_meta_to_table(name,
                                                     dataset_id,
                                                     model_name,
                                                     model_version,
                                                     job_id,
                                                     start_time,
                                                     end_time,
                                                     metric_type,
                                                     uri,
                                                     tags,
                                                     metric_description,
                                                     properties,
                                                     type(self).__name__)
            metric_meta_table.save()
            return MetricMeta(uuid=metric_meta_table.uuid,
                              name=name,
                              dataset_id=dataset_id,
                              model_name=model_name,
                              model_version=model_version,
                              job_id=job_id,
                              start_time=start_time,
                              end_time=end_time,
                              metric_type=metric_type,
                              uri=uri,
                              tags=tags,
                              metric_description=metric_description,
                              properties=properties)
        except Exception as e:
            raise AIFlowException('Registered metric meta failed!'
                                  'Error: {}'.format(str(e)))

    def delete_metric_meta(self, uuid: int):
        try:
            metric_meta_table = MongoMetricMeta.objects(uuid=uuid).first()
            metric_meta_table.is_deleted = TRUE
            metric_meta_table.save()
        except Exception as e:
            raise AIFlowException('delete metric meta failed!'
                                  'Error: {}'.format(str(e)))

    def register_metric_summary(self,
                                metric_id,
                                metric_key,
                                metric_value) -> MetricSummary:
        """
        register metric summary
        :param metric_id: associate with metric meta uuid
        :param metric_key:
        :param metric_value:
        :return:
        """
        try:
            metric_summary_table = metric_summary_to_table(metric_id, metric_key, metric_value, type(self).__name__)
            metric_summary_table.save()
            return MetricSummary(uuid=metric_summary_table.uuid,
                                 metric_id=metric_id,
                                 metric_key=metric_key,
                                 metric_value=metric_value)
        except mongoengine.OperationError as e:
            raise AIFlowException('Registered metric summary failed!'
                                  'Error: {}'.format(str(e)))

    def delete_metric_summary(self, uuid: int):
        try:
            metric_summary_table = MongoMetricSummary.objects(uuid=uuid).first()
            metric_summary_table.is_deleted = TRUE
            metric_summary_table.save()
        except Exception as e:
            raise AIFlowException('delete metric summary failed!'
                                  'Error: {}'.format(str(e)))

    def update_metric_meta(self,
                           uuid,
                           name=None,
                           dataset_id=None,
                           model_name=None,
                           model_version=None,
                           job_id=None,
                           start_time=None,
                           end_time=None,
                           metric_type=None,
                           uri=None,
                           tags=None,
                           metric_description=None,
                           properties=None) -> MetricMeta:
        """
        register metric meta to store
        :param uuid: metric meta unique id
        :param name:
        :param dataset_id: the dataset id of the metric or model metric associate with dataset id
        :param model_name:
        :param model_version: if then model metric, associate with model version id
        :param job_id: the job_id which create the metric
        :param start_time:
        :param end_time:
        :param metric_type: MetricType DATASET or MODEL
        :param uri: the metric uri
        :param tags: such as flink,tensorflow
        :param metric_description:
        :param properties:
        :return:
        """
        try:
            metric_meta_table: MongoMetricMeta = MongoMetricMeta.objects(uuid=uuid, is_deleted__ne=TRUE).first()
            if name is not None:
                metric_meta_table.name = name
            if dataset_id is not None:
                metric_meta_table.dataset_id = dataset_id
            if model_name is not None:
                metric_meta_table.model_name = model_name
            if model_version is not None:
                metric_meta_table.model_version = model_version
            if job_id is not None:
                metric_meta_table.job_id = job_id
            if start_time is not None:
                metric_meta_table.start_time = start_time
            if end_time is not None:
                metric_meta_table.end_time = end_time
            if metric_type is not None:
                metric_meta_table.metric_type = metric_type.value
            if uri is not None:
                metric_meta_table.uri = uri
            if tags is not None:
                metric_meta_table.tags = tags
            if metric_description is not None:
                metric_meta_table.metric_description = metric_description
            if properties is not None and properties != {}:
                metric_meta_table.properties = str(properties)
            metric_meta_table.save()
            return table_to_metric_meta(metric_meta_table)
        except Exception as e:
            raise AIFlowException('Registered metric meta failed!'
                                  'Error: {}'.format(str(e)))

    def update_metric_summary(self,
                              uuid,
                              metric_id=None,
                              metric_key=None,
                              metric_value=None) -> MetricSummary:
        """
        register metric summary
        :param uuid: metric summary unique id
        :param metric_id: associate with metric meta uuid
        :param metric_key:
        :param metric_value:
        :return:
        """
        try:
            metric_summary_table = MongoMetricSummary.objects(uuid=uuid, is_deleted__ne=TRUE).first()
            if metric_id is not None:
                metric_summary_table.metric_id = metric_id
            if metric_key is not None:
                metric_summary_table.metric_key = metric_key
            if metric_value is not None:
                metric_summary_table.metric_value = metric_value
            metric_summary_table.save()
            return table_to_metric_summary(metric_summary_table)
        except Exception as e:
            raise AIFlowException('Registered metric summary failed!'
                                  'Error: {}'.format(str(e)))

    def get_metric_meta(self, name) -> Union[None, MetricMeta]:
        """
        get dataset metric
        :param name:
        :return:
        """
        try:
            metric_meta_table = MongoMetricMeta.objects(name=name, is_deleted__ne=TRUE).first()

            if metric_meta_table is None:
                return None
            else:
                _logger.info("Get dataset metric.")
                return table_to_metric_meta(metric_meta_table)

        except Exception as e:
            raise AIFlowException('Get metric meta  '
                                  'Error: {}'.format(str(e)))

    def get_dataset_metric_meta(self, dataset_id) -> Union[None, MetricMeta, List[MetricMeta]]:
        """
        get dataset metric
        :param dataset_id:
        :return:
        """
        try:
            metric_meta_tables = MongoMetricMeta.objects(dataset_id=dataset_id,
                                                         metric_type=MetricType.DATASET.value,
                                                         is_deleted__ne=TRUE)

            if len(metric_meta_tables) == 0:
                return None
            elif len(metric_meta_tables) == 1:
                _logger.info("Get dataset metric.")
                metric_meta_table = metric_meta_tables[0]
                return table_to_metric_meta(metric_meta_table)
            else:
                _logger.info("Get dataset metric.")
                res = []
                for metric_meta_table in metric_meta_tables:
                    res.append(table_to_metric_meta(metric_meta_table))
                return res
        except Exception as e:
            raise AIFlowException('Get metric meta  '
                                  'Error: {}'.format(str(e)))

    def get_model_metric_meta(self, model_name, model_version) -> Union[None, MetricMeta, List[MetricMeta]]:
        """
        get model metric
        :param model_name:
        :param model_version:
        :return:
        """
        try:
            metric_meta_tables = MongoMetricMeta.objects(model_name=model_name,
                                                         model_version=model_version,
                                                         metric_type=MetricType.MODEL.value,
                                                         is_deleted__ne=TRUE)

            if len(metric_meta_tables) == 0:
                return None
            elif len(metric_meta_tables) == 1:
                metric_meta_table = metric_meta_tables[0]
                return table_to_metric_meta(metric_meta_table)
            else:
                result = []
                for metric_meta_table in metric_meta_tables:
                    result.append(table_to_metric_meta(metric_meta_table))
                return result
        except Exception as e:
            raise AIFlowException('Get metric meta  '
                                  'Error: {}'.format(str(e)))

    def get_metric_summary(self, metric_id) -> Optional[List[MetricSummary]]:
        """
        get metric summary
        :param metric_id:
        :return:
        """
        try:
            metric_summary_tables = MongoMetricSummary.objects(metric_id=metric_id,
                                                               is_deleted__ne=TRUE)

            if len(metric_summary_tables) == 0:
                return None
            else:
                _logger.info("Get metric summary.")
                res = []
                for metric_summary_table in metric_summary_tables:
                    res.append(table_to_metric_summary(metric_summary_table))
                return res
        except Exception as e:
            raise AIFlowException('Get metric summary  '
                                  'Error: {}'.format(str(e)))

    """member api"""

    def list_living_members(self, ttl_ms) -> List[Member]:
        try:
            member_models = MongoMember.objects(update_time__gte=time.time_ns() / 1000000 - ttl_ms)
            return [Member(m.version, m.server_uri, int(m.update_time)) for m in member_models]
        except Exception as e:
            raise AIFlowException("List living AIFlow Member Error.") from e

    def update_member(self, server_uri, server_id):
        try:
            member = MongoMember.objects(server_uri=server_uri).first()
            if member is None:
                member = MongoMember()
                member.version = 1
                member.server_uri = server_uri
                member.update_time = time.time_ns() / 1000000
                member.uuid = server_id
                member.save()
            else:
                if member.uuid != server_id:
                    raise Exception("The server uri '%s' is already exists in the storage!" %
                                    server_uri)
                member.version += 1
                member.update_time = time.time_ns() / 1000000
                member.save()
        except Exception as e:
            raise AIFlowException("Update AIFlow Member Error.") from e

    def clear_dead_members(self, ttl_ms):
        try:
            members = MongoMember.objects(update_time__lt=time.time_ns() / 1000000 - ttl_ms)
            if len(members) == 0:
                return None
            else:
                for member in members:
                    member.delete()
        except Exception as e:
            raise AIFlowException("Clear dead AIFlow Member Error.") from e


def _compare_example_fields(support_type, data_type, data_format, description, batch_uri, stream_uri,
                            create_time, update_time, properties, name_list, type_list, catalog_name,
                            catalog_type, catalog_database, catalog_connection_uri, catalog_table,
                            catalog_version, before_example) -> bool:
    return support_type == before_example.support_type and data_type == before_example.data_type \
           and data_format == before_example.data_format and description == before_example.description \
           and batch_uri == before_example.batch_uri and stream_uri == before_example.stream_uri \
           and create_time == before_example.create_time and update_time == before_example.update_time \
           and properties == before_example.properties and name_list == before_example.schema.name_list \
           and type_list == before_example.schema.type_list and catalog_name == before_example.catalog_name \
           and catalog_type == before_example.catalog_type and catalog_database == before_example.catalog_database \
           and catalog_connection_uri == before_example.catalog_connection_uri \
           and catalog_table == before_example.catalog_table and catalog_version == before_example.catalog_version


def _compare_artifact_fields(data_format, description, batch_uri, stream_uri, create_time,
                             update_time, properties, before_artifact):
    return data_format == before_artifact.data_format and description == before_artifact.description \
           and batch_uri == before_artifact.batch_uri and stream_uri == before_artifact.stream_uri \
           and create_time == before_artifact.create_time and update_time == before_artifact.update_time \
           and properties == before_artifact.properties


def _compare_model_fields(model_type, model_desc, before_model):
    return model_type == before_model.model_type and model_desc == before_model.model_desc


def _compare_model_relation_fields(project_id, before_model_relation):
    return project_id == before_model_relation.project_id


def _compare_project_fields(uri, properties, user, password, project_type, before_project):
    return uri == before_project.uri and properties == before_project.properties and user == before_project.user \
           and password == before_project.password and project_type == before_project.project_type


class MongoStoreConnManager(object):
    _instance = None
    _conns = {}
    _connected_uris = set()
    _closed_uris = set()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = object.__new__(cls, *args, **kwargs)
        return cls._instance

    def connect(self, db_uri, db_name, db_alias=MONGO_DB_ALIAS_META_SERVICE):
        if db_uri not in self._conns:
            self._conns[db_uri] = mongoengine.connect(db_name, host=db_uri, alias=db_alias)
            self._connected_uris.add(db_uri)

    def disconnect(self, db_uri, db_alias=MONGO_DB_ALIAS_META_SERVICE):
        if db_uri in self._conns:
            mongoengine.disconnect(alias=db_alias)
            self._connected_uris.remove(db_uri)
            self._closed_uris.add(db_uri)

    def disconnect_all(self):
        current_connected_uris = self._connected_uris.copy()
        for uri in current_connected_uris:
            self.disconnect(uri)

    def drop(self, db_uri_without_auth):
        db_uri = f'{db_uri_without_auth}?authSource=admin'
        if db_uri in self._conns:
            _, _, _, _, db = parse_mongo_uri(db_uri_without_auth)
            self._conns[db_uri].drop_database(db)

    def drop_all(self):
        current_connected_uris = self._connected_uris.copy()
        for db_uri in current_connected_uris:
            self.drop(db_uri)
