"""Credential-free public observations and explicitly synthetic delivery tests.

This is not the private-account exporter or the trading engine. Only the
notify command imports a notifier or reads notification credentials.
"""
import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

import requests

from app.market.sina import SinaProvider
from app.market.global_markets import (
    ERROR_CODES as GLOBAL_PROVIDER_ERRORS,
    FredTreasuryProvider,
    GlobalMarketDataError,
    INSTRUMENTS,
    SourceObservation,
    YahooGlobalMarketProvider,
)
from app.analysis.industry import DEFAULT_INDUSTRY_POLICY, rank_industries
from app.domain.evidence import EvidenceStatus
from app.market.board_http import EastMoneyBoardClient
from app.market.industries import AkShareIndustryProvider


SHANGHAI = timezone(timedelta(hours=8))
STAGES = {'morning': '09:00', 'midday': '11:30', 'decision': '14:30', 'closing': '16:10'}
PUBLIC_SYMBOLS = {
    'SH.000001': '上证指数', 'SZ.399001': '深证成指',
    'SZ.399006': '创业板指', 'SH.000688': '科创50',
    'SH.000905': '中证500', 'SH.000300': '沪深300',
    'SH.000016': '上证50', 'SZ.399005': '中小100',
}
LEGACY_SCHEMA_VERSION = 'public-trial/v1'
FROZEN_SCHEMA_VERSION = 'public-trial/v2'
SCHEMA_VERSION = 'public-trial/v3'
DATA_CLASSIFICATION = 'public_market_and_synthetic_test'
_ERRORS = {'NO_QUOTE', 'PROVIDER_UNAVAILABLE', 'INVALID_QUOTE'}
GLOBAL_SYMBOLS = tuple(INSTRUMENTS)
_GLOBAL_ERRORS = frozenset(GLOBAL_PROVIDER_ERRORS) | {
    'PROVIDER_UNAVAILABLE', 'INVALID_OBSERVATION', 'INVALID_SOURCE_TIME',
    'TREASURY_SOURCES_UNAVAILABLE', 'NOT_COLLECTED_FOR_STAGE',
}
_GLOBAL_FIELDS = (
    'category', 'symbol', 'name', 'value', 'previous_value', 'value_unit',
    'change', 'change_unit', 'source', 'source_as_of', 'session_date',
    'freshness', 'error_code',
)
_TITLES = {'morning': '晨报', 'midday': '午盘变化', 'decision': '核心决策', 'closing': '收盘复盘'}
_TIMING_LABELS = {
    'manual_replay': '手动演练',
    'on_time': '按计划完成',
    'early': '提前执行',
    'late': '延迟完成',
    'non_weekday': '非工作日执行',
}
_MARKET_LABELS = {
    'available_unverified': '公开行情已获取，来源时间待核验',
    'partial': '部分公开行情暂不可用',
    'unavailable': '公开行情暂不可用',
}
_QUOTE_ERROR_LABELS = {code: '暂不可用' for code in _ERRORS}
_GLOBAL_FRESHNESS_LABELS = {
    'RECENT': '数据较新',
    'DELAYED_OR_HOLIDAY': '可能因延迟或休市未更新',
    'STALE': '数据已陈旧，请谨慎核对',
}
_GLOBAL_STATUS_LABELS = {
    'complete': '完整性：8项全球行情均已获取。',
    'partial': '完整性：部分全球行情暂不可用，其余项目照常展示。',
    'unavailable': '完整性：全球行情暂不可用。',
}
INDUSTRY_NOT_COLLECTED = 'NOT_COLLECTED_FOR_STAGE'
INDUSTRY_ITEMS_LIMIT = 8
INDUSTRY_RENDER_ITEMS = 5
_INDUSTRY_ERRORS = frozenset({
    'PROVIDER_UNAVAILABLE', 'NETWORK_UNAVAILABLE', 'MALFORMED_RESPONSE',
    'NO_USABLE_DATA', 'DATE_MISMATCH',
})
_INDUSTRY_CAUSE_LABELS = {
    'PROVIDER_UNAVAILABLE': '行业数据源暂时不可用',
    'NETWORK_UNAVAILABLE': '行业数据源暂时不可用',
    'MALFORMED_RESPONSE': '行业数据格式异常',
    'NO_USABLE_DATA': '行业数据不足或已过期',
    'DATE_MISMATCH': '行业数据时间不一致',
}
_INDUSTRY_ITEM_FIELDS = (
    'rank', 'code', 'name', 'score',
    'return_1d_pct', 'return_5d_pct', 'return_20d_pct',
)
_INDUSTRY_BLOCK_FIELDS = frozenset({'status', 'as_of', 'error', 'items'})


def _global_value(record):
    if record['symbol'] == '^TNX':
        return f"{record['value']:.2f}%"
    units = {'GC=F': ' 美元/盎司', 'CL=F': ' 美元/桶'}
    return f"{record['value']:,.2f}{units.get(record['symbol'], ' 点')}"


def _global_change(record):
    return (f"{record['change']:+.1f} bp" if record['symbol'] == '^TNX'
            else f"{record['change']:+.2f}%")


def _global_time(record):
    if record['source'] == 'fred_dgs10':
        return f"数据日 {record['session_date'][5:]}"
    observed = _strict_source_time(record['source_as_of']).astimezone(SHANGHAI)
    return f'截至 {observed:%m-%d %H:%M}'


def _render_global_market(payload):
    if payload['schema_version'] == LEGACY_SCHEMA_VERSION:
        return ['隔夜全球市场尚未接入，不判断强弱。']
    market = payload['global_market']
    lines = ['🌍 隔夜全球市场', _GLOBAL_STATUS_LABELS[market['status']]]
    for record in market['quotes']:
        if record['error_code'] is not None:
            lines.append(f"- {record['name']}：暂不可用")
        else:
            lines.append(f"- {record['name']}：{_global_value(record)}；较前值 "
                         f"{_global_change(record)}；{_global_time(record)}；"
                         f"{_GLOBAL_FRESHNESS_LABELS[record['freshness']]}")
    return lines


def _invalid():
    # Never embed a caller's payload, endpoint or provider exception.
    raise ValueError('INVALID_PUBLIC_TRIAL') from None


def _now():
    return datetime.now(SHANGHAI)


def _local_time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(SHANGHAI)


def _parse_time(value):
    if type(value) is not str or len(value) > 40:
        _invalid()
    try:
        result = _local_time(datetime.fromisoformat(value))
    except (ValueError, TypeError, OverflowError):
        _invalid()
    if result.isoformat() != value:
        _invalid()
    return result


def _metadata(stage, requested_at, generated_at, execution_mode, *, schema_version=SCHEMA_VERSION):
    if type(stage) is not str or stage not in STAGES:
        _invalid()
    if execution_mode not in ('manual_replay', 'scheduled'):
        _invalid()
    requested_at, generated_at = _local_time(requested_at), _local_time(generated_at)
    if generated_at < requested_at:
        _invalid()
    hour, minute = (int(part) for part in STAGES[stage].split(':'))
    slot = requested_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if execution_mode == 'manual_replay':
        timing = 'manual_replay'
    elif requested_at.weekday() >= 5:
        timing = 'non_weekday'
    elif requested_at < slot:
        timing = 'early'
    elif generated_at > slot + timedelta(minutes=15):
        timing = 'late'
    else:
        timing = 'on_time'
    if schema_version not in (LEGACY_SCHEMA_VERSION, FROZEN_SCHEMA_VERSION, SCHEMA_VERSION):
        _invalid()
    return {
        'schema_version': schema_version, 'data_classification': DATA_CLASSIFICATION,
        'purpose': 'delivery_test_not_investment_advice', 'stage': stage,
        'execution_mode': execution_mode, 'requested_at': requested_at.isoformat(),
        'generated_at': generated_at.isoformat(), 'scheduled_for': slot.isoformat(),
        'timing_status': timing,
        'coverage': {
            'global_markets': ('NOT_IMPLEMENTED' if schema_version == LEGACY_SCHEMA_VERSION
                               else 'PUBLIC_OBSERVATIONS'),
            'industry_ranking': ('PUBLIC_OBSERVATIONS'
                                 if schema_version == SCHEMA_VERSION
                                 else 'NOT_IMPLEMENTED'),
            'morning_baseline': 'MISSING', 'multi_timeframe': 'NOT_IMPLEMENTED',
            'prediction_pairing': 'MISSING', 'exchange_calendar': 'NOT_IMPLEMENTED',
        },
        'synthetic_account': {
            'id': 'SIMULATED-ONLY',
            'positions': [{'symbol': 'DEMO_A', 'weight_pct': 10}, {'symbol': 'DEMO_B', 'weight_pct': 5}],
            'cash_pct': 85,
        },
        'decision': {
            'action': 'WAIT', 'position_change_pct': 0, 'score': None,
            'manual_confirmation_required': True, 'auto_trade_enabled': False,
        },
        'real_account_trial_day': False,
    }


def _finite_number(value, *, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (not positive or value > 0))


def _quote_record(code, price=None, change=None, error=None):
    return {
        'code': code, 'name': PUBLIC_SYMBOLS[code], 'price': price, 'change_pct': change,
        # Sina's short index format does NOT provide a reliable source time.
        'source_as_of': None, 'freshness': 'UNKNOWN', 'error_code': error,
    }


def _collect_quote(provider, code):
    try:
        quote = provider.fetch(code)
    except Exception:
        return _quote_record(code, error='PROVIDER_UNAVAILABLE')
    if quote is None:
        return _quote_record(code, error='NO_QUOTE')
    try:
        if (type(quote.code) is not str or quote.code != code
                or not _finite_number(quote.price, positive=True)
                or not _finite_number(quote.change)):
            return _quote_record(code, error='INVALID_QUOTE')
        return _quote_record(code, float(quote.price), float(quote.change))
    except Exception:
        return _quote_record(code, error='INVALID_QUOTE')


def _market(quotes):
    count = sum(quote['error_code'] is None for quote in quotes)
    status = ('available_unverified' if count == len(PUBLIC_SYMBOLS)
              else 'partial' if count else 'unavailable')
    return {'source': 'sina_public', 'status': status, 'quotes': quotes}


def _global_record(symbol, *, value=None, previous_value=None, change=None,
                   source=None, source_as_of=None, session_date=None,
                   freshness=None, error_code=None):
    entry = INSTRUMENTS[symbol]
    return {
        'category': entry.category, 'symbol': symbol, 'name': entry.name,
        'value': value, 'previous_value': previous_value,
        'value_unit': entry.value_unit, 'change': change,
        'change_unit': 'bp' if symbol == '^TNX' else 'pct',
        'source': source, 'source_as_of': source_as_of,
        'session_date': session_date, 'freshness': freshness,
        'error_code': error_code,
    }


def _global_failure(symbol, error_code):
    return _global_record(symbol, error_code=error_code)


def _provider_error(error):
    if isinstance(error, GlobalMarketDataError) and error.code in GLOBAL_PROVIDER_ERRORS:
        return error.code
    return 'PROVIDER_UNAVAILABLE'


def _fetch_global_observations(global_provider, treasury_fallback):
    observations = []
    for symbol in GLOBAL_SYMBOLS:
        try:
            if global_provider is None:
                raise RuntimeError
            observation = global_provider.fetch(symbol)
        except Exception as error:
            if symbol != '^TNX':
                observations.append((symbol, None, _provider_error(error), None))
                continue
            try:
                if treasury_fallback is None:
                    raise RuntimeError
                observation = treasury_fallback.fetch(symbol)
            except Exception:
                observations.append((symbol, None, 'TREASURY_SOURCES_UNAVAILABLE', None))
                continue
            observations.append((symbol, observation, None, 'fred_dgs10'))
            continue
        observations.append((symbol, observation, None, 'yahoo_chart'))
    return observations


def _strict_date(value):
    if type(value) is not str or len(value) != 10:
        raise ValueError
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError
    return parsed


def _strict_source_time(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError
    return parsed


class _FutureSourceTime(ValueError):
    pass


def _freshness(source_time, generated_at):
    age = generated_at.astimezone(timezone.utc) - source_time.astimezone(timezone.utc)
    if age < -timedelta(minutes=5):
        raise _FutureSourceTime
    if age <= timedelta(hours=36):
        return 'RECENT'
    if age <= timedelta(hours=120):
        return 'DELAYED_OR_HOLIDAY'
    return 'STALE'


def _observation_record(symbol, observation, generated_at, *, expected_source=None):
    try:
        if (type(observation) is not SourceObservation or observation.symbol != symbol
                or (expected_source is not None and observation.source != expected_source)):
            raise ValueError
        if (not _finite_number(observation.value, positive=True)
                or not _finite_number(observation.previous_value, positive=True)):
            raise ValueError
        session_day = _strict_date(observation.session_date)
        if observation.source == 'yahoo_chart':
            source_time = _strict_source_time(observation.source_as_of)
        elif observation.source == 'fred_dgs10' and symbol == '^TNX' and observation.source_as_of is None:
            source_time = datetime.combine(session_day, time(23, 59, 59), tzinfo=SHANGHAI)
        else:
            raise ValueError
        freshness = _freshness(source_time, generated_at)
        value, previous = float(observation.value), float(observation.previous_value)
        change = ((value - previous) * 100 if symbol == '^TNX'
                  else (value / previous - 1) * 100)
        if not math.isfinite(change):
            raise ValueError
        return _global_record(
            symbol, value=value, previous_value=previous, change=change,
            source=observation.source, source_as_of=observation.source_as_of,
            session_date=observation.session_date, freshness=freshness,
        )
    except _FutureSourceTime:
        return _global_failure(symbol, 'INVALID_SOURCE_TIME')
    except (AttributeError, TypeError, ValueError, OverflowError):
        return _global_failure(symbol, 'INVALID_OBSERVATION')


def _global_market(records, *, stage):
    if stage != 'morning':
        status = 'not_collected'
    else:
        count = sum(record['error_code'] is None for record in records)
        status = ('complete' if count == len(GLOBAL_SYMBOLS)
                  else 'partial' if count else 'unavailable')
    return {'status': status, 'quotes': records}


def _market_date(reference):
    """The last closed mainland weekday from a weekday-only rule."""
    day = reference.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _industry_not_collected():
    return {'status': INDUSTRY_NOT_COLLECTED, 'as_of': None, 'error': None, 'items': []}


def _industry_failure(error_code):
    if error_code not in _INDUSTRY_ERRORS:
        _invalid()
    return {'status': 'FAILED', 'as_of': None, 'error': error_code, 'items': []}


def _industry_item(rank, code, name, score, return_1d_pct, return_5d_pct, return_20d_pct):
    return {
        'rank': rank, 'code': code, 'name': name, 'score': score,
        'return_1d_pct': return_1d_pct, 'return_5d_pct': return_5d_pct,
        'return_20d_pct': return_20d_pct,
    }


def _industry_block(industry_provider):
    """Fail-closed: only a READY ranking is published; anything else is one cause line."""
    now = _now()
    try:
        fetch = industry_provider.fetch(now, _market_date(now))
        observations = fetch.observations
        if type(observations) is not tuple:
            return _industry_failure('MALFORMED_RESPONSE')
        ranking = rank_industries(observations, now, DEFAULT_INDUSTRY_POLICY)
    except Exception:
        return _industry_failure('PROVIDER_UNAVAILABLE')
    if ranking.stamp.status is not EvidenceStatus.READY or not ranking.items:
        return _industry_failure('NO_USABLE_DATA')
    items = [
        _industry_item(
            item.rank, item.code, item.name, float(item.score),
            float(item.observation.return_1d_pct),
            float(item.observation.return_5d_pct),
            float(item.observation.return_20d_pct),
        )
        for item in ranking.items[:INDUSTRY_ITEMS_LIMIT]
    ]
    return {
        'status': ranking.stamp.status.value,
        'as_of': ranking.stamp.as_of.isoformat(),
        'error': None,
        'items': items,
    }


def build_snapshot(stage, *, provider, requested_at, generated_at=None,
                   execution_mode='manual_replay', global_provider=None,
                   treasury_fallback=None, industry_provider=None):
    # Validate intent BEFORE network requests. Capture completion afterwards,
    # without moving the original requested date when collection crosses midnight.
    _metadata(stage, requested_at, requested_at if generated_at is None else generated_at, execution_mode)
    quotes = [_collect_quote(provider, code) for code in PUBLIC_SYMBOLS]
    raw_global = (_fetch_global_observations(global_provider, treasury_fallback)
                  if stage == 'morning' else None)
    completed_at = _now() if generated_at is None else generated_at
    result = _metadata(stage, requested_at, completed_at, execution_mode)
    result['market'] = _market(quotes)
    if raw_global is None:
        records = [_global_failure(symbol, 'NOT_COLLECTED_FOR_STAGE')
                   for symbol in GLOBAL_SYMBOLS]
    else:
        records = [(_global_failure(symbol, error) if error is not None
                    else _observation_record(
                        symbol, observation, _local_time(completed_at),
                        expected_source=expected_source))
                   for symbol, observation, error, expected_source in raw_global]
    result['global_market'] = _global_market(records, stage=stage)
    result['industries'] = (_industry_block(industry_provider) if stage == 'morning'
                            else _industry_not_collected())
    validate_snapshot(result)
    return result


def _same(actual, expected):
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return actual.keys() == expected.keys() and all(_same(actual[key], value) for key, value in expected.items())
    if type(expected) is list:
        return len(actual) == len(expected) and all(_same(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _validate_domestic(payload, expected):
    quotes = payload['market']['quotes']
    if type(quotes) is not list or len(quotes) != len(PUBLIC_SYMBOLS):
        _invalid()
    checked = []
    for code, quote in zip(PUBLIC_SYMBOLS, quotes):
        if type(quote) is not dict:
            _invalid()
        error = quote['error_code']
        if error is None:
            if (not _finite_number(quote['price'], positive=True)
                    or not _finite_number(quote['change_pct'])):
                _invalid()
            checked.append(_quote_record(code, quote['price'], quote['change_pct']))
        elif type(error) is str and error in _ERRORS:
            checked.append(_quote_record(code, error=error))
        else:
            _invalid()
    expected['market'] = _market(checked)


def _validate_global(payload, expected):
    market = payload['global_market']
    if type(market) is not dict or market.keys() != {'status', 'quotes'}:
        _invalid()
    records = market['quotes']
    if type(records) is not list or len(records) != len(GLOBAL_SYMBOLS):
        _invalid()
    checked = []
    generated_at = _parse_time(payload['generated_at'])
    stage = payload['stage']
    for symbol, record in zip(GLOBAL_SYMBOLS, records):
        if type(record) is not dict or tuple(record) != _GLOBAL_FIELDS:
            _invalid()
        if record['symbol'] != symbol:
            _invalid()
        if stage != 'morning':
            rebuilt = _global_failure(symbol, 'NOT_COLLECTED_FOR_STAGE')
            if not _same(record, rebuilt):
                _invalid()
            checked.append(rebuilt)
            continue
        error = record['error_code']
        if error is None:
            observation = SourceObservation(
                symbol=record['symbol'], value=record['value'],
                previous_value=record['previous_value'],
                source_as_of=record['source_as_of'], session_date=record['session_date'],
                source=record['source'],
            )
            rebuilt = _observation_record(symbol, observation, generated_at)
            if rebuilt['error_code'] is not None or not _same(record, rebuilt):
                _invalid()
        elif (type(error) is str and error in _GLOBAL_ERRORS
              and error != 'NOT_COLLECTED_FOR_STAGE'):
            rebuilt = _global_failure(symbol, error)
            if not _same(record, rebuilt):
                _invalid()
        else:
            _invalid()
        checked.append(rebuilt)
    expected['global_market'] = _global_market(checked, stage=stage)


def _validate_industry(payload, expected):
    industries = payload.get('industries')
    if type(industries) is not dict or set(industries.keys()) != _INDUSTRY_BLOCK_FIELDS:
        _invalid()
    status = industries['status']
    if status not in (INDUSTRY_NOT_COLLECTED, 'FAILED', 'READY'):
        _invalid()

    if status == INDUSTRY_NOT_COLLECTED:
        if not _same(industries, _industry_not_collected()):
            _invalid()
        expected['industries'] = _industry_not_collected()
        return

    if status == 'FAILED':
        error = industries['error']
        if type(error) is not str or error not in _INDUSTRY_ERRORS:
            _invalid()
        if industries['as_of'] is not None or industries['items'] != []:
            _invalid()
        expected['industries'] = _industry_failure(error)
        return

    # status == 'READY'
    if industries['error'] is not None:
        _invalid()
    _parse_time(industries['as_of'])
    items = industries['items']
    if type(items) is not list or not (1 <= len(items) <= INDUSTRY_ITEMS_LIMIT):
        _invalid()

    expected_ranks = list(range(1, len(items) + 1))
    seen_codes = set()
    rebuilt_items = []
    for expected_rank, item in zip(expected_ranks, items):
        if type(item) is not dict or set(item.keys()) != set(_INDUSTRY_ITEM_FIELDS):
            _invalid()
        rank = item['rank']
        if type(rank) is not int or type(rank) is bool or rank != expected_rank:
            _invalid()
        code = item['code']
        if type(code) is not str or not code or code in seen_codes:
            _invalid()
        seen_codes.add(code)
        name = item['name']
        if type(name) is not str or not name:
            _invalid()
        score = item['score']
        r1 = item['return_1d_pct']
        r5 = item['return_5d_pct']
        r20 = item['return_20d_pct']
        for val in (score, r1, r5, r20):
            if type(val) is bool or not _finite_number(val):
                _invalid()
        rebuilt_items.append(_industry_item(
            rank, code, name, score, r1, r5, r20
        ))

    expected['industries'] = {
        'status': status,
        'as_of': industries['as_of'],
        'error': None,
        'items': rebuilt_items,
    }


def validate_snapshot(payload):
    """Strict allowlist for exact legacy v1 and current v2 snapshots."""
    try:
        if type(payload) is not dict:
            _invalid()
        schema_version = payload['schema_version']
        if schema_version not in (LEGACY_SCHEMA_VERSION, FROZEN_SCHEMA_VERSION,
                                  SCHEMA_VERSION):
            _invalid()
        expected = _metadata(
            payload['stage'], _parse_time(payload['requested_at']),
            _parse_time(payload['generated_at']), payload['execution_mode'],
            schema_version=schema_version,
        )
        _validate_domestic(payload, expected)
        if schema_version != LEGACY_SCHEMA_VERSION:
            _validate_global(payload, expected)
        if schema_version == SCHEMA_VERSION:
            _validate_industry(payload, expected)
        if not _same(payload, expected):
            _invalid()
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        _invalid()


def render_report(payload):
    validate_snapshot(payload)
    stage = payload['stage']
    generated_at = _parse_time(payload['generated_at'])
    report_time = (
        f'{generated_at.year}年{generated_at.month}月{generated_at.day}日 '
        f'{generated_at:%H:%M}'
    )
    timing = _TIMING_LABELS[payload['timing_status']]
    if payload['execution_mode'] == 'scheduled':
        timing = f'定时演练（{timing}）'
    lines = [
        f"🔔 {STAGES[stage]} {_TITLES[stage]}", '',
        f"北京时间：{report_time}",
        f"国内指数状态：{_MARKET_LABELS[payload['market']['status']]}",
        f"本次模式：{timing}", '',
    ]
    if stage == 'morning':
        lines += _render_global_market(payload)
        industry = payload.get('industries')
        if industry is None:
            lines += ['', '行业排序与评分：尚未接入。', '']
        elif industry['status'] == 'READY':
            as_of = _parse_time(industry['as_of'])
            lines += ['', '🔥 今日重点行业', f"数据时间：{as_of:%m-%d %H:%M}"]
            for item in industry['items'][:INDUSTRY_RENDER_ITEMS]:
                lines.append(f"{item['rank']}. {item['name']}　评分：{item['score']:.1f}")
            lines += ['仅为公开行业观测，未读取你的持仓。', '']
        else:
            lines += ['', f"行业排序：{_INDUSTRY_CAUSE_LABELS.get(industry['error'], '本时段不采集行业数据')}，因此今日不做行业强弱判断。", '']
        lines += ['以下只是公共国内指数观测，不代表你的持仓：', '']
        for quote in payload['market']['quotes']:
            value = (f"{quote['price']:.4f}（{quote['change_pct']:+.2f}%）"
                     if quote['error_code'] is None else _QUOTE_ERROR_LABELS[quote['error_code']])
            lines.append(f"- {quote['name']}：{value}")
        lines += ['', '持仓扫描：本次为合成演练，未读取你的真实账户。']
    elif stage == 'midday':
        lines += [
            '相对当天晨报：缺少可靠基线，暂无法比较；不把累计成本收益写成午盘变化。',
            '资金流与行业评分变化：本时段不重新采集行业数据，也未与晨报配对，暂不补仓。',
        ]
    elif stage == 'decision':
        lines += [
            '标的：DEMO_A / DEMO_B（合成示例）；综合评分：未接入。',
            '周线 / 日线 / 120分钟 / 30分钟 / 5分钟：缺少已验证的结构数据。',
            '14:30 时当日下午120分钟K线尚未闭合，不能据此声称确认二买。',
            '建议仓位变动：0%；风险：来源时间未知、评分与多周期依据不足。',
        ]
    else:
        lines += [
            '事前判断 → 今日走势 → 对错评价：缺少配对记录，暂无法比较。',
            '模型调整：不做调整；不把一次合成演练当作模型学习或真实日验收。',
        ]
    lines += [
        '', '【当前建议】',
        'WAIT｜暂不操作',
        '如需交易，必须由你核对真实账户后人工确认。',
        '', '【风险说明】',
        '本消息只使用公开行情并采用合成账户演练，属于非交易信号，不构成投资建议，也不计入五日真实账户验收。',
        '国内指数行情源未提供可靠时间戳，其新鲜度待核验；工作日调度也不等于交易日历。',
    ]
    return '\n'.join(lines)


def _bundle(snapshots):
    return '📊 投资辅助提醒\n\n' + '\n\n──────────\n\n'.join(
        render_report(snapshot) for snapshot in snapshots
    ) + '\n'


def _is_link(path):
    if not path.exists() and not path.is_symlink():
        return False
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _check_directory(directory, *, allow_git=False):
    directory = Path(directory).absolute()
    if any(_is_link(path) for path in [directory, *directory.parents]):
        _invalid()
    if directory.exists():
        if not directory.is_dir():
            _invalid()
        for path in directory.iterdir():
            if _is_link(path):
                _invalid()
            if path.name == '.git' and allow_git and path.is_dir():
                continue
            if path.name == 'README.md' and path.is_file():
                continue
            if path.name != 'latest' or not path.is_dir():
                _invalid()
            for child in path.iterdir():
                if (_is_link(child) or not child.is_file()
                        or child.name not in {f'{stage}.json' for stage in STAGES}):
                    _invalid()
    return directory


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _read_reports(directory, *, allow_git=False):
    directory = _check_directory(directory, allow_git=allow_git)
    result = []
    for stage in STAGES:
        path = directory / 'latest' / f'{stage}.json'
        if path.exists():
            if path.stat().st_size > 65536:
                _invalid()
            try:
                payload = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_json_pairs)
            except (ValueError, UnicodeError, RecursionError):
                _invalid()
            validate_snapshot(payload)
            if payload['stage'] != stage:
                _invalid()
            result.append(payload)
    return result


def _atomic_write(path, content):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     suffix='.tmp', delete=False, newline='\n') as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_reports(snapshots, output_dir):
    if type(snapshots) is not list or not snapshots:
        _invalid()
    for payload in snapshots:
        validate_snapshot(payload)
        if payload['schema_version'] != SCHEMA_VERSION:
            _invalid()
    if len({payload['stage'] for payload in snapshots}) != len(snapshots):
        _invalid()
    directory = _check_directory(output_dir)
    merged = {payload['stage']: payload for payload in _read_reports(directory)}
    merged.update({payload['stage']: payload for payload in snapshots})
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'latest').mkdir(exist_ok=True)
    for payload in snapshots:
        _atomic_write(directory / 'latest' / f"{payload['stage']}.json",
                      json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    _atomic_write(directory / 'README.md', _bundle([merged[stage] for stage in STAGES if stage in merged]))


class _IsolatedSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.trust_env = False  # No proxy or implicit .netrc credentials.

    def request(self, *args, **kwargs):
        kwargs['allow_redirects'] = False
        response = super().request(*args, **kwargs)
        if 300 <= response.status_code < 400:
            raise requests.RequestException('REDIRECT_BLOCKED')
        return response


def send_test_notification(snapshots, *, notifier=None):
    if type(snapshots) is not list or not snapshots:
        _invalid()
    content = _bundle(snapshots)
    if len(content.encode('utf-8')) > 18000:
        _invalid()
    session = None
    if notifier is None:
        # This import and credential access only happen on explicit notify.
        from app.notify.feishu import FeishuNotifier

        session = _IsolatedSession()
        notifier = FeishuNotifier(session=session)
        if notifier.webhook:
            try:
                url = urlsplit(notifier.webhook)
                safe = (url.scheme == 'https' and url.netloc == 'open.feishu.cn'
                        and re.fullmatch(r'/open-apis/bot/v2/hook/[A-Za-z0-9-]+', url.path)
                        and not url.query and not url.fragment)
            except (ValueError, TypeError):
                safe = False
            if not safe:
                session.close()
                return 'failed'
        elif not all((notifier.app_id, notifier.app_secret, notifier.receive_id)):
            session.close()
            return 'not_configured'
        elif notifier.receive_id_type not in {'open_id', 'user_id', 'union_id', 'email', 'chat_id'}:
            session.close()
            return 'failed'
    try:
        return 'accepted' if notifier.send(content) is True else 'failed'
    except Exception:
        return 'failed'
    finally:
        if session is not None:
            session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Public/synthetic notification trial only')
    commands = parser.add_subparsers(dest='command', required=True)
    collect = commands.add_parser('collect')
    collect.add_argument('--stage', choices=[*STAGES, 'all'], required=True)
    collect.add_argument('--mode', choices=['manual_replay', 'scheduled'], default='manual_replay')
    collect.add_argument('--output', required=True)
    for command in ('validate', 'summary', 'notify'):
        commands.add_parser(command).add_argument('--input', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'collect':
            if args.stage == 'all' and args.mode != 'manual_replay':
                _invalid()
            # Validate output before any network traffic.
            _check_directory(args.output)
            requested_at = _now()
            with _IsolatedSession() as session:
                provider = SinaProvider(session=session)
                global_provider = YahooGlobalMarketProvider(session=session)
                treasury_fallback = FredTreasuryProvider(session=session)
                industry_provider = AkShareIndustryProvider(
                    client=EastMoneyBoardClient(session=session, timeout=10))
                snapshots = [build_snapshot(
                    stage, provider=provider, global_provider=global_provider,
                    treasury_fallback=treasury_fallback,
                    industry_provider=industry_provider,
                    requested_at=requested_at,
                    execution_mode=args.mode)
                             for stage in (STAGES if args.stage == 'all' else [args.stage])]
            write_reports(snapshots, args.output)
            for payload in snapshots:
                print(f"stage={payload['stage']} market_status={payload['market']['status']} trading_ready=false")
        else:
            snapshots = _read_reports(args.input, allow_git=args.command != 'notify')
            if not snapshots:
                _invalid()
            if args.command == 'summary':
                _atomic_write(Path(args.input) / 'README.md', _bundle(snapshots))
            elif args.command == 'validate':
                readme = Path(args.input) / 'README.md'
                if readme.stat().st_size > 65536 or readme.read_text(encoding='utf-8') != _bundle(snapshots):
                    _invalid()
                print('public_schema=valid trading_ready=false real_account_trial_day=false')
            else:
                status = send_test_notification(snapshots)
                print(f'notification_status={status}')
                return {'accepted': 0, 'failed': 1, 'not_configured': 2}[status]
        return 0
    except Exception:
        print('PUBLIC_TRIAL_FAILED')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
