"""Offline schema checks. Live PostgreSQL migration testing is separate."""
import importlib.util
from io import StringIO
from pathlib import Path
import unittest

from alembic import command
from alembic.config import Config
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex, CreateTable

from infrastructure.db import Base
from infrastructure.db.social_models import User


ROOT = Path(__file__).resolve().parents[1]


class SchemaRecorder:
    """Reconstruct frozen migration DDL without using the ORM metadata."""
    def __init__(self):
        self.metadata = sa.MetaData()

    def f(self, name):
        return sa.schema.conv(name)

    def create_table(self, name, *elements, **kwargs):
        return sa.Table(name, self.metadata, *elements, **kwargs)

    def create_index(self, name, table_name, columns, **kwargs):
        table = self.metadata.tables[table_name]
        sa.Index(name, *(table.c[c] for c in columns), **kwargs)

    def drop_index(self, name, table_name, **kwargs):
        table = self.metadata.tables[table_name]
        index = next(index for index in table.indexes if index.name == name)
        table.indexes.remove(index)

    def drop_constraint(self, name, table_name, **kwargs):
        table = self.metadata.tables[table_name]
        constraint = next(constraint for constraint in table.constraints if constraint.name == name)
        table.constraints.remove(constraint)

    def create_check_constraint(self, name, table_name, condition, **kwargs):
        table = self.metadata.tables[table_name]
        table.append_constraint(sa.CheckConstraint(condition, name=name))

    def add_column(self, table_name, column):
        self.metadata.tables[table_name].append_column(column)

    def execute(self, statement):
        # Data migration behavior is exercised against PostgreSQL separately.
        pass


def ddl(metadata):
    dialect = postgresql.dialect()
    result = {}
    for table in metadata.sorted_tables:
        # Constraint ordering isn't semantically significant.
        result[table.name] = sorted(
            line.strip().rstrip(',')
            for line in str(CreateTable(table).compile(dialect=dialect)).splitlines()
            if line.strip()
        )
        for index in table.indexes:
            result[index.name] = str(CreateIndex(index).compile(dialect=dialect))
    return result


class SchemaTests(unittest.TestCase):
    def test_frozen_migrations_match_models(self):
        recorder = SchemaRecorder()
        for path in sorted((ROOT / 'infrastructure/db/migrations/versions').glob('*.py')):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.op = recorder
            module.upgrade()
        self.assertEqual(ddl(recorder.metadata), ddl(Base.metadata))
        self.assertEqual(len(recorder.metadata.tables), 21)

    def test_upgrade_and_social_downgrade_sql(self):
        output = StringIO()
        config = Config(str(ROOT / 'alembic.ini'), output_buffer=output)
        command.upgrade(config, 'head', sql=True)
        sql = output.getvalue()
        self.assertIn('FOREIGN KEY(sender_plan_id, event_id)', sql)
        self.assertIn('FOREIGN KEY(recipient_plan_id, event_id)', sql)
        self.assertIn('first_user_id < second_user_id', sql)
        self.assertIn('CREATE UNIQUE INDEX uq_notifications_pending_digest', sql)
        self.assertIn('DROP CONSTRAINT ck_users_active_profile_complete;', sql)
        self.assertNotIn('ck_users_ck_users_', sql)
        self.assertIn('ADD COLUMN reacted_at TIMESTAMP WITH TIME ZONE', sql)
        output.seek(0)
        output.truncate()
        command.downgrade(config, '0004_profile_photo_optional:0003_multiple_primary_tags', sql=True)
        self.assertIn('DROP CONSTRAINT ck_users_active_profile_complete;', output.getvalue())
        self.assertNotIn('ck_users_ck_users_', output.getvalue())
        output.seek(0)
        output.truncate()
        command.downgrade(config, '0002_social:0001_catalog', sql=True)
        sql = output.getvalue()
        self.assertIn('DROP TABLE users;', sql)
        self.assertNotIn('DROP TABLE events;', sql)

    def test_non_repeating_views_and_reactions(self):
        self.assertEqual(set(Base.metadata.tables['companion_views'].primary_key.columns.keys()),
                         {'viewer_id', 'shown_user_id', 'event_id'})
        self.assertEqual(set(Base.metadata.tables['event_reactions'].primary_key.columns.keys()),
                         {'user_id', 'event_id'})

    def test_event_can_have_multiple_primary_tags(self):
        indexes = Base.metadata.tables['event_tags'].indexes
        self.assertNotIn('uq_event_tags_primary', {index.name for index in indexes})

    def test_async_safe_relationships(self):
        configure_mappers()
        for name in ('plans', 'event_reactions', 'tag_weights'):
            self.assertEqual(User.__mapper__.relationships[name].lazy, 'raise')


if __name__ == '__main__':
    unittest.main()
