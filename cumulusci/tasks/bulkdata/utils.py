import os
import datetime
import requests
import tempfile
import time
import unicodecsv
import xml.etree.ElementTree as ET

from contextlib import contextmanager
from sqlalchemy import types
from sqlalchemy import event
from sqlalchemy import Table
from sqlalchemy import create_engine
from sqlalchemy import Column
from sqlalchemy import MetaData
from sqlalchemy import Integer
from sqlalchemy import Unicode
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import create_session

from cumulusci.utils import convert_to_snake_case
from cumulusci.core.tasks import BaseTask
from cumulusci.core.utils import ordered_yaml_load
from cumulusci.core.exceptions import BulkDataException


@contextmanager
def download_file(uri, bulk_api):
    """Download the bulk API result file for a single batch"""
    resp = requests.get(uri, headers=bulk_api.headers(), stream=True)
    with tempfile.TemporaryFile("w+b") as f:
        for chunk in resp.iter_content(chunk_size=None):
            f.write(chunk)
        f.seek(0)
        yield f


def process_incoming_rows(f, record_type=None):
    if record_type and not isinstance(record_type, bytes):
        record_type = record_type.encode("utf-8")
    for line in f:
        if record_type:
            yield line.rstrip() + b"," + record_type + b"\n"
        else:
            yield line


def get_lookup_key_field(lookup, sf_field):
    return lookup.get("key_field", convert_to_snake_case(sf_field))


# Create a custom sqlalchemy field type for sqlite datetime fields which are stored as integer of epoch time
class EpochType(types.TypeDecorator):
    impl = types.Integer

    epoch = datetime.datetime(1970, 1, 1, 0, 0, 0)

    def process_bind_param(self, value, dialect):
        return int((value - self.epoch).total_seconds()) * 1000

    def process_result_value(self, value, dialect):
        if value is not None:
            return self.epoch + datetime.timedelta(seconds=value / 1000)


# Listen for sqlalchemy column_reflect event and map datetime fields to EpochType
@event.listens_for(Table, "column_reflect")
def setup_epoch(inspector, table, column_info):
    if isinstance(column_info["type"], types.DateTime):
        column_info["type"] = EpochType()


class BulkJobTaskMixin(object):
    def _job_state_from_batches(self, job_id):
        uri = "{}/job/{}/batch".format(self.bulk.endpoint, job_id)
        response = requests.get(uri, headers=self.bulk.headers())
        return self._parse_job_state(response.content)

    def _parse_job_state(self, xml):
        tree = ET.fromstring(xml)
        statuses = [el.text for el in tree.iterfind(".//{%s}state" % self.bulk.jobNS)]
        state_messages = [
            el.text for el in tree.iterfind(".//{%s}stateMessage" % self.bulk.jobNS)
        ]

        if "Not Processed" in statuses:
            return "Aborted", None
        elif "InProgress" in statuses or "Queued" in statuses:
            return "InProgress", None
        elif "Failed" in statuses:
            return "Failed", state_messages

        return "Completed", None

    def _wait_for_job(self, job_id):
        while True:
            job_status = self.bulk.job_status(job_id)
            self.logger.info(
                "    Waiting for job {} ({}/{})".format(
                    job_id,
                    job_status["numberBatchesCompleted"],
                    job_status["numberBatchesTotal"],
                )
            )
            result, messages = self._job_state_from_batches(job_id)
            if result != "InProgress":
                break
            time.sleep(10)
        self.logger.info("Job {} finished with result: {}".format(job_id, result))
        if result == "Failed":
            for state_message in messages:
                self.logger.error("Batch failure message: {}".format(state_message))

        return result

    def _sql_bulk_insert_from_csv(self, conn, table, columns, data_file):
        if conn.dialect.name in ("postgresql", "psycopg2"):
            # psycopg2 (the postgres driver) supports COPY FROM
            # to efficiently bulk insert rows in CSV format
            with conn.connection.cursor() as cursor:
                cursor.copy_expert(
                    "COPY {} ({}) FROM STDIN WITH (FORMAT CSV)".format(
                        table, ",".join(columns)
                    ),
                    data_file,
                )
        else:
            # For other db drivers we need to use standard SQL
            # -- this is optimized for ease of implementation
            # rather than performance and may need more work.
            reader = unicodecsv.DictReader(data_file, columns)
            table = self.metadata.tables[table]
            rows = list(reader)
            if rows:
                conn.execute(table.insert().values(rows))
        self.session.flush()


class BaseBatchDataTask(BaseTask):
    """Abstract base class for any class that generates data using a SQL DB."""

    task_docs = """
    Use the `num_records` option to specify how many records to generate.
    Use the `mapping` option to specify a mapping file.
    """

    task_options = {
        "num_records": {
            "description": "How many records to generate: total number of opportunities.",
            "required": True,
        },
        "mapping": {"description": "A mapping YAML file to use", "required": True},
        "database_url": {
            "description": "A path to put a copy of the sqlite database (for debugging)",
            "required": False,
        },
    }

    def _run_task(self):
        mapping_file = os.path.abspath(self.options["mapping"])
        database_url = self.options.get("database_url")
        if not database_url:
            sqlite_path = "generated_data.db"
            self.logger.info("No database URL: creating sqlite file %s" % sqlite_path)
            database_url = "sqlite:///" + sqlite_path

        num_records = int(self.options["num_records"])
        self._generate_data(database_url, mapping_file, num_records)

    def _generate_data(self, db_url, mapping_file_path, num_records):
        """Generate all of the data"""
        with open(mapping_file_path, "r") as f:
            mappings = ordered_yaml_load(f)

        session, engine, base = self.init_db(db_url, mappings)
        self.generate_data(session, engine, base, num_records)
        session.commit()

    def init_db(self, db_url, mappings):
        engine = create_engine(db_url)
        metadata = MetaData()
        metadata.bind = engine
        for mapping in mappings.values():
            create_table(mapping, metadata)
        metadata.create_all()
        base = automap_base(bind=engine, metadata=metadata)
        base.prepare(engine, reflect=True)
        session = create_session(bind=engine, autocommit=False)
        return session, engine, base

    def generate_data(self, session, engine, base):
        raise NotImplementedError("generate_data method not impelemented")


def create_table(mapping, metadata):
    # Provide support for legacy mappings which used the OID as the pk but
    # default to using an autoincrementing int pk and a separate sf_id column
    fields = []
    mapping["oid_as_pk"] = bool(mapping.get("fields", {}).get("Id"))
    if mapping["oid_as_pk"]:
        id_column = mapping["fields"]["Id"]
        fields.append(Column(id_column, Unicode(255), primary_key=True))
    else:
        fields.append(Column("id", Integer(), primary_key=True, autoincrement=True))
    for field in fields_for_mapping(mapping):
        if mapping["oid_as_pk"] and field["sf"] == "Id":
            continue
        fields.append(Column(field["db"], Unicode(255)))
    if "record_type" in mapping:
        fields.append(Column("record_type", Unicode(255)))
    t = Table(mapping["table"], metadata, *fields)
    if t.exists():
        raise BulkDataException("Table already exists: {}".format(mapping["table"]))
    return t


def fields_for_mapping(mapping):
    fields = []
    for sf_field, db_field in mapping.get("fields", {}).items():
        fields.append({"sf": sf_field, "db": db_field})
    for sf_field, lookup in mapping.get("lookups", {}).items():
        fields.append({"sf": sf_field, "db": get_lookup_key_field(lookup, sf_field)})
    return fields
