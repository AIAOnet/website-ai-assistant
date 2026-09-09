"""Persistent atomic provider-call budgets measured without provider-specific billing data."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .database_maintenance import Migration, apply_migrations, open_database, require_columns


class UsageBudgetExceeded(RuntimeError): pass


class UsageBudgetChange(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    generation_daily: int = Field(ge=0, le=1000000)
    generation_monthly: int = Field(ge=0, le=10000000)
    embedding_daily: int = Field(ge=0, le=10000000)
    embedding_monthly: int = Field(ge=0, le=100000000)
    confirmed: Literal[True]


DEFAULTS = (1000, 20000, 10000, 200000)


def _schema_v1(database):
    database.execute("CREATE TABLE usage_budget_settings(singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation_daily INTEGER NOT NULL, generation_monthly INTEGER NOT NULL, embedding_daily INTEGER NOT NULL, embedding_monthly INTEGER NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0))")
    database.execute("INSERT INTO usage_budget_settings VALUES(1,?,?,?,?,0)", DEFAULTS)
    database.execute("CREATE TABLE usage_reservations(reservation_id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL CHECK(category IN ('generation','embedding')), units INTEGER NOT NULL CHECK(units>0), reserved_at REAL NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL)")
    database.execute("CREATE INDEX usage_period ON usage_reservations(category,day,month)")


class UsageBudget:
    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(open_database(self.path)) as database, database:
            apply_migrations(database, 'usage_budget', (Migration(1, 'provider usage budgets', _schema_v1),))
            require_columns(database, 'usage_budget_settings', {'singleton','generation_daily','generation_monthly','embedding_daily','embedding_monthly','revision'})
            require_columns(database, 'usage_reservations', {'reservation_id','category','units','reserved_at','day','month'})

    def _now(self):
        value = self.clock()
        if value.tzinfo is None: raise ValueError('Budget clock must be timezone-aware')
        return value.astimezone(timezone.utc)

    def reserve(self, category, units=1):
        if category not in {'generation','embedding'} or not isinstance(units, int) or units < 1:
            raise ValueError('Invalid usage reservation')
        now = self._now(); day = now.date().isoformat(); month = day[:7]
        with closing(open_database(self.path)) as database:
            database.execute('BEGIN IMMEDIATE')
            try:
                settings = database.execute('SELECT generation_daily,generation_monthly,embedding_daily,embedding_monthly FROM usage_budget_settings WHERE singleton=1').fetchone()
                daily, monthly = settings[:2] if category == 'generation' else settings[2:]
                used_day = database.execute('SELECT COALESCE(SUM(units),0) FROM usage_reservations WHERE category=? AND day=?', (category,day)).fetchone()[0]
                used_month = database.execute('SELECT COALESCE(SUM(units),0) FROM usage_reservations WHERE category=? AND month=?', (category,month)).fetchone()[0]
                if units > daily-used_day or units > monthly-used_month:
                    database.rollback(); raise UsageBudgetExceeded(f'{category} budget exhausted')
                database.execute('INSERT INTO usage_reservations(category,units,reserved_at,day,month) VALUES(?,?,?,?,?)', (category,units,now.timestamp(),day,month))
                database.execute('DELETE FROM usage_reservations WHERE month<?', (month,))
                database.commit()
            except Exception:
                if database.in_transaction: database.rollback()
                raise

    def status(self):
        now = self._now(); day = now.date().isoformat(); month = day[:7]
        with closing(open_database(self.path)) as database:
            values = database.execute('SELECT generation_daily,generation_monthly,embedding_daily,embedding_monthly,revision FROM usage_budget_settings WHERE singleton=1').fetchone()
            used = {(category,period): units for category,period,units in database.execute("SELECT category,day,SUM(units) FROM usage_reservations WHERE day=? GROUP BY category,day UNION ALL SELECT category,month,SUM(units) FROM usage_reservations WHERE month=? GROUP BY category,month", (day,month))}
        gd,gm,ed,em,revision = values
        def row(category,daily,monthly):
            return {'daily_limit':daily,'daily_used':used.get((category,day),0),'monthly_limit':monthly,'monthly_used':used.get((category,month),0)}
        return {'generation':row('generation',gd,gm),'embedding':row('embedding',ed,em),'revision':revision,'measurement':{'generation':'provider_calls','embedding':'input_texts'},'timezone':'UTC'}

    def save(self, change):
        values = (change.generation_daily,change.generation_monthly,change.embedding_daily,change.embedding_monthly)
        if values[1] < values[0] or values[3] < values[2]: raise ValueError('Monthly limits must be at least daily limits')
        with closing(open_database(self.path)) as database, database:
            database.execute('UPDATE usage_budget_settings SET generation_daily=?,generation_monthly=?,embedding_daily=?,embedding_monthly=?,revision=revision+1 WHERE singleton=1', values)
        return self.status()
