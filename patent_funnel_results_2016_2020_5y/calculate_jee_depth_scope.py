#!/usr/bin/env python3
"""Jee Depth/Scope: 기존 working.sqlite + 전체 인용 원본 재사용.

Python 3.9 이상, 추가 설치 없음.
실행: python calculate_jee_depth_scope.py
또는 python calculate_jee_depth_scope.py --db ".../working.sqlite" --citation ".../g_us_patent_citation.tsv.zip"

기존 3,815쌍 전체에서 공동특허별 Depth_A/Scope_A/Depth_B/Scope_B를 계산하고,
사후 공동특허 인용이 있던 609쌍 및 방향별 partner-Depth 가중합을 별도 집계합니다.
원본 인용 파일은 두 번 순차 읽습니다. 기존 DB와 원본은 변경하지 않습니다.
완료 후 jee_depth_scope_summary.zip만 공유하면 됩니다.

Depth_X(i) = 공동특허 i의 각 선행인용을 X의 사전 특허들이 반복 인용한 횟수 합 / i의 선행인용 수
Scope_X(i) = i의 선행인용 중 X의 사전 특허에서 인용한 적 없는 것의 수 / i의 선행인용 수
사전 창과 사후 창은 기존 funnel의 공동특허별 이동 창을 유지합니다.
권리자가 정확히 하나인 특허, 미국 등록특허 인용, 모든 인용 주체라는 기존 제한을 유지합니다.
Jee의 가족 단위/협력 사례 분석을 그대로 복제하는 코드는 아닙니다.
"""

import argparse
import bisect
import collections
import csv
import datetime as dt
import io
import json
import sqlite3
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path

VERSION = 1


def log(message):
    print('[' + dt.datetime.now().strftime('%H:%M:%S') + '] ' + message, flush=True)


def shift(value, years):
    value = dt.date.fromisoformat(value)
    try:
        return value.replace(year=value.year + years).isoformat()
    except ValueError:
        return value.replace(year=value.year + years, day=28).isoformat()


def ordinal(value):
    return dt.date.fromisoformat(value).toordinal()


def choose_file(explicit, kind):
    if explicit:
        result = Path(explicit).expanduser().resolve()
    else:
        roots = {Path.cwd(), Path(__file__).resolve().parent}
        candidates = set()
        for root in roots:
            if kind == 'db':
                candidates.update(root.glob('working.sqlite'))
                candidates.update(root.glob('patent_funnel_results*/working.sqlite'))
            else:
                candidates.update(root.glob('g_us_patent_citation*.tsv'))
                candidates.update(root.glob('g_us_patent_citation*.zip'))
        candidates = {p.resolve() for p in candidates if p.is_file()}
        if len(candidates) == 1:
            result = candidates.pop()
        else:
            title = '기존 funnel의 working.sqlite 선택' if kind == 'db' else '전체 g_us_patent_citation 원본 선택 (sample ZIP 제외)'
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                try:
                    selected = filedialog.askopenfilename(title=title)
                finally:
                    root.destroy()
            except Exception:
                selected = input(title + ' 경로: ').strip().strip('"')
            if not selected:
                raise ValueError('파일 선택을 취소했습니다.')
            result = Path(selected).expanduser().resolve()
    if not result.is_file():
        raise FileNotFoundError(result)
    return result


@contextmanager
def citation_rows(path):
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            matches = [name for name in archive.namelist()
                       if Path(name).name == 'g_us_patent_citation.tsv']
            if len(matches) != 1:
                raise ValueError('전체 g_us_patent_citation.tsv(.zip)를 선택하세요.')
            with archive.open(matches[0]) as raw, io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as stream:
                yield csv.reader(stream, delimiter='\t')
    else:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            yield csv.reader(stream, delimiter='\t')


def source_settings(path):
    identity = path.parent / 'input_settings.json'
    if identity.is_file():
        return json.loads(identity.read_text(encoding='utf-8-sig'))
    summary = path.parent / 'summary.json'
    if summary.is_file():
        return {'settings': json.loads(summary.read_text(encoding='utf-8-sig'))['settings']}
    raise ValueError('working.sqlite와 같은 폴더에 기존 input_settings.json 또는 summary.json이 필요합니다.')


def population(source, settings, expected_pairs, expected_events):
    tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {'cp', 'solos', 'entities', 'pair_first', 'backward', 'events', 'done'}
    if not required <= tables:
        raise ValueError('완료된 기존 funnel DB가 아닙니다. 없는 테이블: ' + str(required - tables))
    if not source.execute("SELECT 1 FROM done WHERE name='citations'").fetchone():
        raise ValueError('기존 funnel의 인용 처리가 완료되지 않았습니다.')
    query = '''SELECT c.pid,c.a,c.b,c.filed,c.deadline,c.bg_a,c.bg_b
        FROM cp c JOIN backward b ON b.pid=c.pid
        WHERE c.bg_a>0 AND c.bg_b>0 AND c.deadline<=? ORDER BY c.pid,c.a,c.b'''
    links = list(source.execute(query, (settings['cutoff'],)))
    pairs = {(a, b) for pid, a, b, filed, deadline, bg_a, bg_b in links}
    eligible = {(pid, a, b) for pid, a, b, filed, deadline, bg_a, bg_b in links}
    events = {}
    for pid, a, b, firm, n in source.execute('SELECT pid,a,b,firm,n FROM events'):
        if (pid, a, b) in eligible:
            if firm not in (a, b) or n <= 0:
                raise ValueError('기존 events에 유효하지 않은 방향/횟수가 있습니다.')
            events[(pid, a, b, firm)] = n
    event_pairs = {(a, b) for pid, a, b, firm in events}
    if expected_pairs is not None and len(pairs) != expected_pairs:
        raise ValueError(f'기존 표본과 다릅니다: {len(pairs):,}쌍 (예상 {expected_pairs:,}). DB와 조건을 확인하세요.')
    if expected_events is not None and len(event_pairs) != expected_events:
        raise ValueError(f'기존 인용 표본과 다릅니다: {len(event_pairs):,}쌍 (예상 {expected_events:,}).')
    if not links:
        raise ValueError('분석 대상이 없습니다.')
    return links, events


def contexts_from_links(links, background_years):
    contexts = {}
    for pid, a, b, filed, deadline, bg_a, bg_b in links:
        lower = ordinal(shift(filed, -background_years))
        upper = ordinal(filed)
        for firm, n_background in ((a, bg_a), (b, bg_b)):
            value = (pid, firm, lower, upper, n_background)
            key = (pid, firm)
            if key in contexts and contexts[key] != value:
                raise ValueError('같은 공동특허/기업의 배경 창이 일치하지 않습니다.')
            contexts[key] = value
    return list(contexts.values())


def finished(work, name):
    return work.execute('SELECT 1 FROM done WHERE name=?', (name,)).fetchone() is not None


def finish(work, name):
    work.execute('INSERT OR REPLACE INTO done VALUES(?)', (name,))
    work.commit()


def scan_joint_references(work, citation, wanted):
    if finished(work, 'joint_scan'):
        log('1/5 공동특허 선행인용: 완료 단계 재사용')
        return
    log(f'1/5 공동특허 {len(wanted):,}건의 선행인용 수집: 인용 원본 1차 읽기')
    work.execute('DELETE FROM joint_refs')
    work.commit()
    batch = []
    kept = 0
    with citation_rows(citation) as reader:
        header = next(reader)
        ci, ri = header.index('patent_id'), header.index('citation_patent_id')
        for n, row in enumerate(reader, 1):
            if row[ci] in wanted and row[ri]:
                batch.append((row[ci], row[ri]))
                kept += 1
            if len(batch) >= 20000:
                work.executemany('INSERT OR IGNORE INTO joint_refs VALUES(?,?)', batch)
                batch.clear()
            if n % 5000000 == 0:
                work.commit()
                log(f'  1차 인용 {n:,}행 / 관련 행 {kept:,}')
    work.executemany('INSERT OR IGNORE INTO joint_refs VALUES(?,?)', batch)
    found = {r[0] for r in work.execute('SELECT DISTINCT pid FROM joint_refs')}
    if found != wanted:
        work.commit()
        raise ValueError(f'대상 공동특허 {len(wanted-found):,}건의 인용이 없습니다. 원본이 불완전하거나 DB와 다릅니다.')
    finish(work, 'joint_scan')


def load_background_solos(source, contexts):
    bounds = {}
    for pid, firm, lo, hi, n in contexts:
        previous = bounds.get(firm)
        bounds[firm] = (min(lo, previous[0]), max(hi, previous[1])) if previous else (lo, hi)
    source.execute('CREATE TEMP TABLE needed_firms(firm INTEGER PRIMARY KEY,lo TEXT,hi TEXT)')
    source.executemany('INSERT INTO needed_firms VALUES(?,?,?)', [
        (firm, dt.date.fromordinal(lo).isoformat(), dt.date.fromordinal(hi).isoformat())
        for firm, (lo, hi) in bounds.items()])
    result = {}
    for pid, firm, filed in source.execute('''SELECT s.pid,s.firm,s.filed FROM needed_firms f
        JOIN solos s ON s.firm=f.firm AND s.filed>=f.lo AND s.filed<f.hi'''):
        result[pid] = (firm, ordinal(filed))
    source.execute('DROP TABLE needed_firms')
    return result


def scan_background_references(work, source, citation, contexts):
    if finished(work, 'background_scan'):
        log('2/5 사전 특허 선행인용: 완료 단계 재사용')
        return
    log('2/5 사전 단독특허 연결 및 인용 원본 2차 읽기')
    solos = load_background_solos(source, contexts)
    references = {r[0] for r in work.execute('SELECT DISTINCT cited FROM joint_refs')}
    log(f'  관련 사전 단독특허 {len(solos):,}건 / 대조할 선행특허 {len(references):,}건')
    work.execute('DELETE FROM background_edges')
    work.commit()
    batch = []
    kept = 0
    with citation_rows(citation) as reader:
        header = next(reader)
        ci, ri = header.index('patent_id'), header.index('citation_patent_id')
        for n, row in enumerate(reader, 1):
            if row[ri] in references:
                info = solos.get(row[ci])
                if info:
                    batch.append((row[ci], row[ri], info[0], info[1]))
                    kept += 1
            if len(batch) >= 20000:
                work.executemany('INSERT OR IGNORE INTO background_edges VALUES(?,?,?,?)', batch)
                batch.clear()
            if n % 5000000 == 0:
                work.commit()
                log(f'  2차 인용 {n:,}행 / 관련 행 {kept:,}')
    work.executemany('INSERT OR IGNORE INTO background_edges VALUES(?,?,?,?)', batch)
    log('  배경 인용 검색 인덱스 생성')
    work.execute('CREATE INDEX IF NOT EXISTS background_by_firm_ref_day ON background_edges(firm,cited,day)')
    finish(work, 'background_scan')


def calculate_metrics(work, contexts):
    if finished(work, 'metrics'):
        log('3/5 Depth/Scope: 완료 단계 재사용')
        return
    log('3/5 공동특허 × 기업별 Depth/Scope 계산')
    references = collections.defaultdict(list)
    for pid, cited in work.execute('SELECT pid,cited FROM joint_refs'):
        references[pid].append(cited)
    by_firm = collections.defaultdict(list)
    for value in contexts:
        by_firm[value[1]].append(value)
    work.execute('DELETE FROM metrics')
    batch = []
    processed = 0
    for firm, values in sorted(by_firm.items()):
        citation_dates = collections.defaultdict(list)
        for cited, day in work.execute('SELECT cited,day FROM background_edges WHERE firm=? ORDER BY cited,day', (firm,)):
            citation_dates[cited].append(day)
        for pid, _, lo, hi, n_background in values:
            refs = references[pid]
            total = len(refs)
            repetition = 0
            new = 0
            for cited in refs:
                dates = citation_dates.get(cited, [])
                count = bisect.bisect_left(dates, hi) - bisect.bisect_left(dates, lo)
                repetition += count
                new += count == 0
            depth = repetition / total
            scope = new / total
            batch.append((pid, firm, total, repetition, new, n_background, depth, scope, depth * scope))
            processed += 1
        if len(batch) >= 1000:
            work.executemany('INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?,?)', batch)
            batch.clear()
            work.commit()
            log(f'  공동특허 × 기업 {processed:,}/{len(contexts):,}개')
    work.executemany('INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?,?)', batch)
    finish(work, 'metrics')


def write_csv(path, header, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def distribution(values):
    return {'n': len(values), 'zero': sum(value == 0 for value in values),
            'min': min(values), 'max': max(values), 'mean': sum(values) / len(values)}


def export(work, source, links, events, contexts, settings, out):
    log('4/5 사후 인용 사건 연결 및 방향별 가중합 집계')
    firms = {firm for pid, firm, lo, hi, n in contexts}
    entities = {firm: (aid, name) for firm, aid, name in source.execute('SELECT firm,aid,name FROM entities') if firm in firms}
    first = {(a, b): date for a, b, date in source.execute('SELECT a,b,first_date FROM pair_first')}
    metrics = {(pid, firm): (total, repeat, new, nbg, depth, scope, comp)
               for pid, firm, total, repeat, new, nbg, depth, scope, comp in work.execute('SELECT * FROM metrics')}
    if len(metrics) != len(contexts):
        raise ValueError('Depth/Scope 계산 행 수가 표본 구조와 다릅니다.')
    joint_rows = []
    directional_rows = []
    pair_data = {}
    denominator_cache = {}
    for pid, a, b, filed, deadline, bg_a, bg_b in links:
        ma, mb = metrics[(pid, a)], metrics[(pid, b)]
        na, nb = events.get((pid, a, b, a), 0), events.get((pid, a, b, b), 0)
        joint_rows.append([pid, *entities[a], *entities[b], filed, deadline,
                           shift(filed, -settings['background_years']), bg_a, bg_b,
                           ma[0], ma[1], ma[2], ma[4], ma[5], ma[6],
                           mb[1], mb[2], mb[4], mb[5], mb[6], na, nb])
        entry = pair_data.setdefault((a, b), {'n_co': 0, 'n_event_co': 0,
                                             'a': [0, 0.0, 0.0, 0.0, 0.0],
                                             'b': [0, 0.0, 0.0, 0.0, 0.0]})
        entry['n_co'] += 1
        entry['n_event_co'] += na > 0 or nb > 0
        for recipient, partner, own, other, n, side in (
                (a, b, ma, mb, na, 'a'), (b, a, mb, ma, nb, 'b')):
            key = (recipient, filed, deadline)
            if key not in denominator_cache:
                denominator_cache[key] = source.execute('''SELECT COUNT(*) FROM solos
                    WHERE firm=? AND filed>? AND filed<=?''', key).fetchone()[0]
            denominator = denominator_cache[key]
            if n > denominator or (n and not denominator):
                raise ValueError('기존 사건 수와 사후 단독특허 분모가 일치하지 않습니다.')
            partner_depth_score = n * other[4]
            own_scope_score = n * own[5]
            own_depth_score = n * own[4]
            partner_scope_score = n * other[5]
            directional_rows.append([pid, *entities[recipient], *entities[partner], filed, deadline,
                own[4], own[5], other[4], other[5], n, denominator,
                partner_depth_score, own_scope_score, own_depth_score, partner_scope_score,
                partner_depth_score / denominator if denominator else None])
            for index, value in enumerate((n, partner_depth_score, own_scope_score, own_depth_score, partner_scope_score)):
                entry[side][index] += value
    pair_rows = []
    counts = collections.Counter()
    for (a, b), entry in sorted(pair_data.items()):
        va, vb = entry['a'], entry['b']
        any_event = va[0] + vb[0] > 0
        positive_a, positive_b = va[1] > 0, vb[1] > 0
        counts['pairs_with_reassembly_events'] += any_event
        counts['pairs_with_positive_partner_depth_weighted_events'] += positive_a or positive_b
        counts['event_pairs_with_zero_partner_depth_weighted_events'] += any_event and not (positive_a or positive_b)
        counts['pairs_with_both_directions_positive_partner_depth'] += positive_a and positive_b
        counts['pairs_with_one_direction_positive_partner_depth'] += positive_a != positive_b
        counts['positive_directions_partner_depth'] += positive_a + positive_b
        counts['positive_directions_own_scope'] += (va[2] > 0) + (vb[2] > 0)
        counts['total_reassembly_events_pair_expanded'] += va[0] + vb[0]
        row = [*entities[a], *entities[b], first.get((a, b)), entry['n_co'], entry['n_event_co']]
        for value in (va, vb):
            row.extend(value)
            row.extend([score / value[0] if value[0] else None for score in value[1:]])
        row.extend([any_event, positive_a, positive_b])
        pair_rows.append(row)
    joint_header = ['joint_patent_id','firm_A_id','firm_A_name','firm_B_id','firm_B_name',
        'joint_filing','follow_end_inclusive','background_start_inclusive','A_background_solo_patents',
        'B_background_solo_patents','distinct_joint_backward_citations','A_repetition_total','A_new_reference_count',
        'Depth_A','Scope_A','Complementarity_A','B_repetition_total','B_new_reference_count',
        'Depth_B','Scope_B','Complementarity_B','A_followup_citations_to_joint','B_followup_citations_to_joint']
    write_csv(out/'joint_patent_depth_scope.csv', joint_header, joint_rows)
    write_csv(out/'directed_joint_outcomes.csv', ['joint_patent_id','recipient_id','recipient_name','partner_id',
        'partner_name','joint_filing','follow_end_inclusive','Depth_recipient','Scope_recipient',
        'Depth_partner','Scope_partner','followup_citations_to_joint','recipient_followup_solo_patents',
        'partner_depth_weighted_events','own_scope_weighted_events','own_depth_weighted_events',
        'partner_scope_weighted_events','custom_partner_depth_score_per_followup_solo'], directional_rows)
    header = ['firm_A_id','firm_A_name','firm_B_id','firm_B_name','first_observed_joint_filing',
              'eligible_joint_patents','joint_patents_with_reassembly_event']
    for label in ('A', 'B'):
        header.extend([label + '_' + field for field in ('reassembly_events','partner_depth_weighted_sum',
            'own_scope_weighted_sum','own_depth_weighted_sum','partner_scope_weighted_sum',
            'partner_depth_event_weighted_mean','own_scope_event_weighted_mean',
            'own_depth_event_weighted_mean','partner_scope_event_weighted_mean')])
    header.extend(['has_reassembly_event','A_positive_partner_depth_weighted_sum','B_positive_partner_depth_weighted_sum'])
    write_csv(out/'pair_depth_scope_summary.csv', header, pair_rows)
    write_csv(out/'event_pairs_depth_scope.csv', header, [row for row in pair_rows if row[-3]])
    summary = {'code_version': VERSION, 'settings': settings,
        'source_paper': 'Su Jung Jee, Intellectual property reassembly: a novel approach to evaluate R&D collaboration outcomes, doi:10.1111/radm.12709, sections 3.2-3.3.',
        'population_pairs': len(pair_data), 'eligible_unique_joint_patents': len({r[0] for r in links}),
        'eligible_joint_patent_pair_links': len(links), 'unique_joint_patent_firm_contexts': len(contexts),
        'joint_patents_with_reassembly_event': len({pid for pid,a,b,firm in events}),
        'joint_patent_pair_links_with_reassembly_event': len({(pid,a,b) for pid,a,b,firm in events}),
        'directed_joint_patent_pair_observations': len(directional_rows), 'event_and_weighted_results': dict(counts),
        'depth_distribution_unique_patent_firm': distribution([value[4] for value in metrics.values()]),
        'scope_distribution_unique_patent_firm': distribution([value[5] for value in metrics.values()]),
        'pair_weighted_sum_denominator': 'None. These are numerators, not final pair-normalized Y labels.',
        'custom_score_unit': 'Per joint patent and recipient: partner Depth times subsequent independent citations to this joint patent, divided by recipient solo patents within this same joint patent follow-up window. This is an adaptation, not Jee original dependent variable.',
        'notes': [
            'All stage-5 pairs retained, including zero subsequent reassembly events.',
            'Depth and Scope are computed separately for each joint patent and each participating firm.',
            'Background: [joint filing minus background years, joint filing). Follow-up: (joint filing, deadline].',
            'Background and follow-up patents have exactly one assignee ID, maintaining the original funnel restriction.',
            'References are all US granted-patent references. No applicant/examiner restriction. Duplicate citing/cited edges counted once.',
            'Background reference patents need not be owned by the partner. Familiarity is previous use in that partner own citations.',
            'Depth can exceed 1; Scope lies in [0,1]; Scope is not 1 minus Depth.',
            'Pair-level weighted sums and means are custom summaries. Jee original analyses use recurrent-event hazard models.',
            'Do not sum per-joint denominators as a pair denominator: follow-up windows overlap.',
            'One joint patent with multiple firms is expanded into unordered pairs. Pair-expanded event totals may count a patent citation for several pairs.',
            'US patent documents, not patent families. Company classification and assignee-ID consolidation remain unchanged.',
            'Patent filing dates proxy knowledge timing; citations can be added during examination after filing.',
            'A positive partner-Depth weighted result is not proof of exclusive knowledge origin, causal learning or economic benefit.'
        ]}
    (out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    definitions = '''계산 기준
기존 3,815쌍에서 공동특허별 5년 사전/5년 사후 창을 유지합니다. 실제 조건은 summary.json을 확인하세요.
Depth_X(i): i의 선행인용 목록을 X의 사전 단독특허들이 인용한 횟수 합 / i의 고유 선행인용 수.
Scope_X(i): i의 선행인용 중 X의 사전 단독특허 인용 목록에 없는 수 / i의 고유 선행인용 수.
범위는 Depth>=0, 0<=Scope<=1. 둘은 이분법이 아니며 Scope=1-Depth도 아닙니다.
기존에 X가 소유한 특허와 대조하는 것이 아니라, X의 사전 특허들이 인용한 참조 목록과 대조합니다.
joint_patent_depth_scope.csv: 공동특허-기업쌍마다 A/B 네 지표와 사후 인용 횟수.
directed_joint_outcomes.csv: 받는 기업과 상대를 분리. 상대 Depth 가중값, 자신의 Scope 가중값 등을 별도 저장.
pair_depth_scope_summary.csv: 모든 쌍. 사건 가중합과 사건수로 나눈 가중평균. 사후 인용 없는 쌍도 유지.
event_pairs_depth_scope.csv: 위와 같은 열에서 사후 공동특허 인용 사건이 있는 쌍만 별도 저장(기존 예상 609쌍).
partner_depth_weighted_sum = sum(상대 Depth_i * 받는 기업의 사후 단독특허가 공동특허 i를 인용한 횟수).
own_scope_weighted_sum = sum(자신의 Scope_i * 같은 사후 인용 횟수).
가중평균은 위 합 / 사후 공동특허 인용 연결 수. 사건 0이면 평균은 공란입니다.
쌍 단위 최종 Y 분모는 확정하지 않습니다. 공동특허마다 이동 창이 달라 사후 특허 분모를 단순 합치면 중복됩니다.
directed_joint_outcomes의 custom 값만 해당 공동특허의 사후 단독특허 수로 나눕니다. 분모 0이면 공란입니다.
가중합/평균 및 custom 값은 현재 연구를 위한 변형이며 Jee 원논문의 반복사건 모형 종속변수와 다릅니다.
한 공동특허의 선행인용 여러 건 중 무엇을 사후 단독특허가 실제로 활용했는지는 이 계산만으로 특정하지 못합니다.
'''
    (out/'definitions.txt').write_text(definitions, encoding='utf-8')
    log('5/5 결과 압축')
    with zipfile.ZipFile(out/'jee_depth_scope_summary.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('summary.json','definitions.txt','joint_patent_depth_scope.csv','directed_joint_outcomes.csv',
                     'pair_depth_scope_summary.csv','event_pairs_depth_scope.csv'):
            archive.write(out/name, name)
    log(f'전체 {len(pair_data):,}쌍 / 사후 인용 {counts["pairs_with_reassembly_events"]:,}쌍')
    log(f'상대 Depth 가중합 양수: {counts["pairs_with_positive_partner_depth_weighted_events"]:,}쌍')
    log('완료: ' + str((out/'jee_depth_scope_summary.zip').resolve()))
    return summary


def run(database, citation, out, expected_pairs=3815, expected_events=609):
    database, citation, out = Path(database).resolve(), Path(citation).resolve(), Path(out).resolve()
    metadata = source_settings(database)
    settings = metadata['settings']
    for key in ('cutoff', 'background_years', 'follow_years'):
        if key not in settings:
            raise ValueError('기존 실행 조건 누락: ' + key)
    original = metadata.get('files', {}).get('g_us_patent_citation', {})
    if original.get('bytes') and original['bytes'] != citation.stat().st_size:
        raise ValueError('선택한 인용 파일의 크기가 기존 funnel 원본과 다릅니다. 같은 전체 원본을 선택하세요.')
    source = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        links, events = population(source, settings, expected_pairs, expected_events)
        contexts = contexts_from_links(links, settings['background_years'])
        out.mkdir(parents=True, exist_ok=True)
        signature = {'version': VERSION, 'settings': settings, 'source': {}, 'expected_pairs': expected_pairs,
                     'expected_events': expected_events}
        for key, path in (('database', database), ('citation', citation)):
            stat = path.stat()
            signature['source'][key] = {'path': str(path), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        identity = out/'input_settings.json'
        if identity.exists() and json.loads(identity.read_text(encoding='utf-8')) != signature:
            raise ValueError('이 결과 폴더의 이전 입력과 다릅니다. --output으로 새 폴더를 지정하세요.')
        identity.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding='utf-8')
        work = sqlite3.connect(out/'jee_working.sqlite')
        try:
            work.executescript('''PRAGMA journal_mode=DELETE; PRAGMA synchronous=NORMAL;
                PRAGMA cache_size=-65536; PRAGMA temp_store=FILE;
                CREATE TABLE IF NOT EXISTS done(name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS joint_refs(pid TEXT,cited TEXT,PRIMARY KEY(pid,cited)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS background_edges(citing TEXT,cited TEXT,firm INTEGER,day INTEGER,
                    PRIMARY KEY(citing,cited)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS metrics(pid TEXT,firm INTEGER,n_refs INTEGER,repetition INTEGER,
                    new_refs INTEGER,n_background INTEGER,depth REAL,scope REAL,complementarity REAL,
                    PRIMARY KEY(pid,firm)) WITHOUT ROWID;''')
            scan_joint_references(work, citation, {row[0] for row in links})
            scan_background_references(work, source, citation, contexts)
            calculate_metrics(work, contexts)
            return export(work, source, links, events, contexts, settings, out)
        finally:
            work.close()
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db')
    parser.add_argument('--citation')
    parser.add_argument('--output', default='jee_depth_scope_results')
    parser.add_argument('--expected-pairs', type=int, default=3815)
    parser.add_argument('--expected-event-pairs', type=int, default=609)
    args = parser.parse_args()
    database = choose_file(args.db, 'db')
    citation = choose_file(args.citation, 'citation')
    log('기존 DB: ' + str(database))
    log('인용 원본: ' + str(citation))
    run(database, citation, args.output, args.expected_pairs, args.expected_event_pairs)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n중단했습니다. 같은 명령으로 실행하면 완료된 읽기 단계를 재사용합니다.', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print('\n오류: ' + str(error), file=sys.stderr)
        sys.exit(1)
