#!/usr/bin/env python3
"""기업 공동특허 표본 흐름 집계. Python 3.9+ / 추가 설치 없음.

실행: python count_patent_funnel.py
원본 4개를 이 파일과 같은 폴더에 두거나 선택창에서 지정:
 g_patent.tsv(.zip), g_application.tsv(.zip),
 g_assignee_disambiguated.tsv(.zip), g_us_patent_citation.tsv(.zip)
원본 전체 파일을 사용하세요. 현대차·기아 sample ZIP을 선택하면 안 됩니다.

기본 조건: 공동출원 2016-01-01~2020-12-31, 배경 5년, 이후 관찰 5년,
자료 기준일 2025-12-31, 배경자료 사용 시작일 2010-01-01.
배경기간은 [공동특허 출원일-5년, 공동특허 출원일), 관찰기간은
(공동특허 출원일, 공동특허 출원일+5년]입니다. 고정 연도 블록이 아닙니다.
디자인 특허도 포함합니다. --utility-only로 실용특허만 분석할 수 있습니다.

기업은 기관명 규칙과 assignee_type으로 1차 분류합니다(확정 분류 아님).
권리자 ID를 기업 식별자로 사용하며 계열사/이름변경 ID는 자동 병합하지 않습니다.
기업만 권리자로 기재된 공동특허에서 모든 2개 기업 조합을 만듭니다.
배경지식과 이후 독립 산출은 전체 권리자 ID가 1개인 단독특허로 한정합니다.
기본 인용은 주체를 구분하지 않는 미국 등록특허 간 인용입니다.
기준일 이후 등록, 철회 표시, 유효 출원일 없는 특허는 제외합니다.

이 코드는 단계별 표본 수를 집계합니다. Depth/Scope/Y 값은 계산하지 않습니다.
기업 쌍별 같은 공동특허에 3~6번 조건을 순차 적용합니다.
인용 사건 없는 쌍도 결과에 남깁니다. 최초 관측 협력일은 조건 적용 전
해당 기업 쌍의 전체 제공 이력에서 구합니다(대학 등 제3자 동반 특허도 포함).
자료 밖의 과거 협력, 미등록/비공개 출원, 아직 축적되지 않은 인용은 알 수 없습니다.

SQLite 중간 DB를 결과 폴더에 저장합니다. 원본은 변경하지 않습니다.
중단 후 같은 명령으로 재실행하면 완료된 단계를 재사용합니다.
인용 파일 처리 중 중단됐다면 그 단계의 읽기는 처음부터 다시 합니다.
큰 DB 파일은 공유할 필요 없습니다. 마지막 result_summary.zip만 보내세요.
"""

import argparse
import collections
import csv
import datetime as dt
import io
import itertools
import json
import re
import sqlite3
import sys
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

UNIVERSITY = re.compile(r'UNIVERSIT|UNIVERZIT|UNIWERSYT|UNIVERSIDAD|COLLEGE|SCHOOL OF|HOCHSCHULE|DAIGAKU|INDUSTRY.*ACADEM|ACADEMIC.*COOPERATION|REGENTS OF|ECOLE|ÉCOLE|POLYTECHNI|POLITECNICO|\bPOSTECH\b|\bKAIST\b|\bUNIST\b|ETH Z|KOREA ADVANCED INSTITUTE|MASSACHUSETTS INSTITUTE OF TECHNOLOGY|CALIFORNIA INSTITUTE OF TECHNOLOGY|GEORGIA INSTITUTE OF TECHNOLOGY|TOKYO INSTITUTE OF TECHNOLOGY|TECHNION|WISCONSIN ALUMNI RESEARCH FOUNDATION|SNU R&DB|OHIO STATE INNOVATION FOUNDATION|GWANGJU INSTITUTE OF SCIENCE|DAEGU GYEONGBUK INSTITUTE OF SCIENCE')
PUBLIC_RESEARCH = re.compile(r'FRAUNHOFER|MAX.?PLANCK|RIKEN|RIKAGAKU|CENTRE NATIONAL DE LA RECHERCHE|CONSEJO SUPERIOR DE INVESTIGACIONES|COMMONWEALTH SCIENTIFIC AND INDUSTRIAL|COMMISSARIAT.*[EÉ]NERG|AGENCY FOR SCIENCE.*RESEARCH|KOREA INSTITUTE OF|KOREA RESEARCH INSTITUTE|ELECTRONICS AND TELECOMMUNICATIONS RESEARCH INSTITUTE|INDUSTRIAL TECHNOLOGY RESEARCH INSTITUTE|NATIONAL INSTITUTE|NATIONAL RESEARCH|NATIONAL LABORATOR|ACADEMY OF SCIENCES|ACADEMIA SINICA|BATTELLE|SRI INTERNATIONAL|INSTITUT PASTEUR|INSTITUT NATIONAL|RESEARCH FOUNDATION|MEDICAL EDUCATION AND RESEARCH|RESEARCH ASSOCIATION')
COMPANY = re.compile(r'\b(?:INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LTD|LIMITED|LLC|LLP|PLC|GMBH|AG|PTY|PTE|SARL|SAS|SRL|SA|BV|NV|SPA|OYJ|OY|AB|KG|KFT|BERHAD|SDN|ULC|SE|BVBA)\b|KABUSHIKI|KAISHA|KABUSIKI|YUGEN|AKTIEBOLAG|AKTIENGESELLSCHAFT|AKTIESELSKAB|GESELLSCHAFT MIT BESCHR|\b(?:L\.P\.|K\.K\.|G\.K\.|S\.A\.|S\.R\.L\.|B\.V\.|N\.V\.|S\.P\.A\.|A/S\b|S/A\b)|株式会社|주식회사')
RESEARCH = re.compile(r'INSTITUT|INSTITUTE|ISTITUTO|LABORATO|ACADEMY|FORSCHUNG|RICERCA|RECERCA')


def classify(name, kind):
    n = re.sub(r'\s+', ' ', name.upper()).strip()
    if not n:
        return 'other_unknown'
    if UNIVERSITY.search(n):
        return 'university'
    if PUBLIC_RESEARCH.search(n):
        return 'research'
    if kind in {'2', '3', '12', '13'} and COMPANY.search(n):
        return 'company'
    if RESEARCH.search(n):
        return 'research'
    return 'other_unknown'


@contextmanager
def read_table(path, name):
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as z:
            matches = [n for n in z.namelist() if Path(n).name == name + '.tsv']
            if len(matches) != 1:
                raise ValueError(f'{path.name}: {name}.tsv 파일이 없습니다. 파일 종류를 확인하세요.')
            with z.open(matches[0]) as b, io.TextIOWrapper(b, encoding='utf-8-sig', newline='') as f:
                yield csv.reader(f, delimiter='\t')
    else:
        with path.open(encoding='utf-8-sig', newline='') as f:
            yield csv.reader(f, delimiter='\t')


def choose(given, stem):
    if given:
        path = Path(given).expanduser().resolve()
    else:
        found = set()
        for folder in {Path.cwd(), Path(__file__).resolve().parent}:
            found.update(p.resolve() for p in folder.glob(stem + '*')
                         if p.is_file() and p.suffix.lower() in {'.zip', '.tsv'})
        if len(found) == 1:
            path = found.pop()
        else:
            value = ''
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk(); root.withdraw()
                value = filedialog.askopenfilename(title=stem + ' 원본 파일 선택')
                root.destroy()
            except Exception:
                value = input(stem + ' 파일 경로: ').strip().strip('"')
            if not value:
                raise ValueError(stem + ': 선택 취소')
            path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with read_table(path, stem) as r:
        h = next(r)
        required = {'patent_id'}
        if stem == 'g_patent': required |= {'patent_date', 'patent_type', 'withdrawn'}
        if stem == 'g_application': required |= {'filing_date'}
        if stem == 'g_assignee_disambiguated': required |= {'assignee_id', 'disambig_assignee_organization', 'assignee_type'}
        if stem == 'g_us_patent_citation': required |= {'citation_patent_id'}
        if not required <= set(h):
            raise ValueError(stem + ': 컬럼이 다른 파일을 선택했습니다.')
    return path


def stamp():
    return dt.datetime.now().strftime('%H:%M:%S')


def log(message):
    print(f'[{stamp()}] {message}', flush=True)


def shift(value, years):
    d = dt.date.fromisoformat(value)
    try:
        return d.replace(year=d.year + years).isoformat()
    except ValueError:
        return d.replace(year=d.year + years, day=28).isoformat()


def valid_date(value):
    try:
        d = dt.date.fromisoformat(value)
        return d.isoformat() if 1800 <= d.year <= 2100 else None
    except ValueError:
        return None


def done(db, name):
    return db.execute('SELECT 1 FROM done WHERE name=?', (name,)).fetchone() is not None


def finish(db, name):
    db.execute('INSERT OR REPLACE INTO done VALUES(?)', (name,)); db.commit()


def setup(db):
    db.executescript('''
    PRAGMA journal_mode=DELETE;
    PRAGMA synchronous=NORMAL;
    PRAGMA cache_size=-65536;
    PRAGMA temp_store=FILE;
    CREATE TABLE IF NOT EXISTS done(name TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS patents(pid TEXT PRIMARY KEY, granted TEXT, kind TEXT, withdrawn INTEGER, filed TEXT) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS entities(firm INTEGER PRIMARY KEY, aid TEXT UNIQUE, name TEXT, category TEXT);
    CREATE TABLE IF NOT EXISTS owners(pid TEXT, firm INTEGER, PRIMARY KEY(pid,firm)) WITHOUT ROWID;
    ''')


def load_patents(db, files):
    if not done(db, 'patents'):
        log('1/7: 특허 기본정보 읽기')
        db.execute('DELETE FROM patents'); batch = []
        with read_table(files['g_patent'], 'g_patent') as r:
            h = next(r); indices = [h.index(c) for c in ['patent_id', 'patent_date', 'patent_type', 'withdrawn']]
            for n, row in enumerate(r, 1):
                p, gd, kind, withdrawn = (row[i] for i in indices)
                batch.append((p, valid_date(gd), kind, int(withdrawn or '0')))
                if len(batch) >= 20000:
                    db.executemany('INSERT OR REPLACE INTO patents(pid,granted,kind,withdrawn) VALUES(?,?,?,?)', batch); batch.clear()
                if n % 1000000 == 0: db.commit(); log(f'  기본정보 {n:,}행')
        db.executemany('INSERT OR REPLACE INTO patents(pid,granted,kind,withdrawn) VALUES(?,?,?,?)', batch)
        finish(db, 'patents')
    if not done(db, 'applications'):
        log('2/7: 출원일 연결'); db.execute('UPDATE patents SET filed=NULL'); batch = []
        with read_table(files['g_application'], 'g_application') as r:
            h = next(r); pi, fi = h.index('patent_id'), h.index('filing_date')
            for n, row in enumerate(r, 1):
                value = valid_date(row[fi])
                if value: batch.append((value, row[pi], value))
                if len(batch) >= 20000:
                    db.executemany('UPDATE patents SET filed=? WHERE pid=? AND (filed IS NULL OR filed>?)', batch); batch.clear()
                if n % 1000000 == 0: db.commit(); log(f'  출원일 {n:,}행')
        db.executemany('UPDATE patents SET filed=? WHERE pid=? AND (filed IS NULL OR filed>?)', batch)
        finish(db, 'applications')


def load_owners(db, files):
    if done(db, 'owners'): return
    log('3/7: 권리자 ID 연결 및 기관명 1차 분류')
    db.execute('DELETE FROM owners'); db.execute('DELETE FROM entities')
    keys = {}; entity_batch = []; owner_batch = []
    with read_table(files['g_assignee_disambiguated'], 'g_assignee_disambiguated') as r:
        h = next(r); ix = [h.index(c) for c in ['patent_id', 'assignee_id', 'disambig_assignee_organization', 'assignee_type']]
        for n, row in enumerate(r, 1):
            pid, aid, name, kind = (row[i] for i in ix)
            if not aid: raise ValueError('빈 assignee_id가 있어 단독/공동 여부를 안전하게 판단할 수 없습니다.')
            key = keys.get(aid)
            if key is None:
                key = len(keys) + 1; keys[aid] = key
                entity_batch.append((key, aid, name, classify(name, kind)))
            owner_batch.append((pid, key))
            if len(owner_batch) >= 20000:
                db.executemany('INSERT INTO entities VALUES(?,?,?,?)', entity_batch); entity_batch.clear()
                db.executemany('INSERT OR IGNORE INTO owners VALUES(?,?)', owner_batch); owner_batch.clear()
            if n % 1000000 == 0: db.commit(); log(f'  권리자 {n:,}행')
    db.executemany('INSERT INTO entities VALUES(?,?,?,?)', entity_batch)
    db.executemany('INSERT OR IGNORE INTO owners VALUES(?,?)', owner_batch)
    db.execute('CREATE INDEX IF NOT EXISTS owners_by_firm ON owners(firm,pid)')
    finish(db, 'owners')


def build_candidates(db, args):
    if done(db, 'candidates'): return
    log('4/7: 기업 쌍 만들기 / 출원 전 5년 단독특허 조건 검사')
    db.executescript('''DROP TABLE IF EXISTS stats; DROP TABLE IF EXISTS solos; DROP TABLE IF EXISTS cp; DROP TABLE IF EXISTS pair_first;
    CREATE TABLE stats(pid TEXT PRIMARY KEY, n INTEGER, n_company INTEGER) WITHOUT ROWID;
    CREATE TABLE solos(pid TEXT PRIMARY KEY, firm INTEGER, filed TEXT) WITHOUT ROWID;
    CREATE TABLE cp(pid TEXT, a INTEGER, b INTEGER, filed TEXT, deadline TEXT, bg_a INTEGER, bg_b INTEGER,
    PRIMARY KEY(pid,a,b)) WITHOUT ROWID;
    CREATE TABLE pair_first(a INTEGER,b INTEGER,first_date TEXT,PRIMARY KEY(a,b)) WITHOUT ROWID;
    ''')
    kind_filter = " AND p.kind='utility'" if args.utility_only else ''
    db.execute('''INSERT INTO stats SELECT o.pid,COUNT(*),SUM(e.category='company') FROM owners o
    JOIN entities e ON e.firm=o.firm JOIN patents p ON p.pid=o.pid
    WHERE p.withdrawn=0 AND p.filed IS NOT NULL AND p.granted<=? AND p.filed<=?''' + kind_filter + ' GROUP BY o.pid',
               (args.cutoff, args.cutoff))
    db.execute('''INSERT INTO solos SELECT o.pid,o.firm,p.filed FROM owners o JOIN stats s ON s.pid=o.pid
    JOIN patents p ON p.pid=o.pid WHERE s.n=1 AND s.n_company=1 AND p.filed>=?''', (args.history_start,))
    db.execute('CREATE INDEX solos_by_firm_date ON solos(firm,filed)')
    query = '''SELECT o.pid,p.filed,o.firm FROM owners o JOIN stats s ON s.pid=o.pid
    JOIN patents p ON p.pid=o.pid WHERE s.n>=2 AND s.n=s.n_company
    AND p.filed>=? AND p.filed<=? ORDER BY o.pid,o.firm'''
    iterator = db.execute(query, (args.cohort_start,args.cohort_end))
    batch=[]; n=0; total=0
    for pid, values in itertools.groupby(iterator, key=lambda r:r[0]):
        members=list(values); filed=members[0][1]; firms=[r[2] for r in members]
        lower=shift(filed,-args.background_years)
        bg={}
        for firm in firms:
            bg[firm]=0 if lower<args.history_start else db.execute(
                'SELECT COUNT(*) FROM solos WHERE firm=? AND filed>=? AND filed<?', (firm,lower,filed)).fetchone()[0]
        deadline=shift(filed,args.follow_years)
        for a,b in itertools.combinations(firms,2):
            batch.append((pid,a,b,filed,deadline,bg[a],bg[b]));total+=1
        n+=1
        if len(batch)>=10000:
            db.executemany('INSERT INTO cp VALUES(?,?,?,?,?,?,?)',batch); batch.clear()
        if n%10000==0: log(f'  공동특허 {n:,}건 / 특허-기업쌍 연결 {total:,}건')
    db.executemany('INSERT INTO cp VALUES(?,?,?,?,?,?,?)',batch)
    db.execute('CREATE INDEX cp_by_pair ON cp(a,b,pid)')
    pairs={(a,b) for a,b in db.execute('SELECT DISTINCT a,b FROM cp')}
    log(f'  분석기간 공동특허 {n:,}건 / 고유 기업 쌍 {len(pairs):,}쌍')
    # First observed collaboration uses all available, valid history; no background/citation filter.
    history=db.execute('''SELECT o.pid,p.filed,o.firm FROM owners o JOIN stats s ON s.pid=o.pid
    JOIN patents p ON p.pid=o.pid JOIN entities e ON e.firm=o.firm
    WHERE s.n>=2 AND e.category='company' ORDER BY o.pid,o.firm''')
    first={}
    for pid,values in itertools.groupby(history,key=lambda r:r[0]):
        members=list(values); filed=members[0][1]
        for pair in itertools.combinations([r[2] for r in members],2):
            if pair in pairs and (pair not in first or filed<first[pair]):first[pair]=filed
    db.executemany('INSERT INTO pair_first VALUES(?,?,?)',[(a,b,d) for (a,b),d in first.items()])
    finish(db,'candidates')


def load_citations(db, files):
    if done(db,'citations'): return
    log('5/7: 대용량 인용 파일 순차 읽기 (압축 해제·전체 메모리 적재 없음)')
    db.executescript('''DROP TABLE IF EXISTS backward; DROP TABLE IF EXISTS forward_edges;
    CREATE TABLE backward(pid TEXT PRIMARY KEY) WITHOUT ROWID;
    CREATE TABLE forward_edges(citing TEXT,cited TEXT,applicant INTEGER,examiner INTEGER,
    PRIMARY KEY(citing,cited)) WITHOUT ROWID;''')
    candidates={p for p, in db.execute('SELECT DISTINCT pid FROM cp WHERE bg_a>0 AND bg_b>0')}
    log(f'  배경 조건을 통과한 공동특허 {len(candidates):,}건의 인용 확인')
    if not candidates: finish(db,'citations'); return
    backward=set(); edges=[]; kept=0
    with read_table(files['g_us_patent_citation'],'g_us_patent_citation') as r:
        h=next(r); ci,ri=h.index('patent_id'),h.index('citation_patent_id')
        ki=h.index('citation_category') if 'citation_category' in h else None
        for n,row in enumerate(r,1):
            citing,cited=row[ci],row[ri]
            if not cited: continue
            if citing in candidates:backward.add(citing)
            if cited in candidates:
                category=row[ki].lower() if ki is not None else ''
                edges.append((citing,cited,int('applicant' in category),int('examiner' in category)));kept+=1
            if len(edges)>=20000:
                db.executemany('''INSERT INTO forward_edges VALUES(?,?,?,?) ON CONFLICT(citing,cited)
                DO UPDATE SET applicant=MAX(applicant,excluded.applicant),examiner=MAX(examiner,excluded.examiner)''',edges);edges.clear()
            if n%5000000==0:db.commit();log(f'  인용 {n:,}행 읽음 / 관련 전방인용 행 {kept:,}건')
    db.executemany('''INSERT INTO forward_edges VALUES(?,?,?,?) ON CONFLICT(citing,cited)
    DO UPDATE SET applicant=MAX(applicant,excluded.applicant),examiner=MAX(examiner,excluded.examiner)''',edges)
    db.executemany('INSERT INTO backward VALUES(?)',[(p,) for p in backward])
    finish(db,'citations')


def write_csv(path, header, rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(header);w.writerows(rows)


def export(db,args,out):
    log('6/7: 기간 내 단독특허의 공동특허 인용 사건 확인')
    db.executescript('''DROP TABLE IF EXISTS events;
    CREATE TABLE events(pid TEXT,a INTEGER,b INTEGER,firm INTEGER,n INTEGER,n_applicant INTEGER,n_examiner INTEGER,
    PRIMARY KEY(pid,a,b,firm)) WITHOUT ROWID;''')
    db.execute('''INSERT INTO events SELECT cp.pid,cp.a,cp.b,so.firm,COUNT(*),SUM(f.applicant),SUM(f.examiner)
    FROM forward_edges f JOIN cp ON cp.pid=f.cited JOIN backward bw ON bw.pid=cp.pid
    JOIN solos so ON so.pid=f.citing AND (so.firm=cp.a OR so.firm=cp.b)
    WHERE cp.bg_a>0 AND cp.bg_b>0 AND cp.deadline<=? AND so.filed>cp.filed AND so.filed<=cp.deadline
    GROUP BY cp.pid,cp.a,cp.b,so.firm''',(args.cutoff,))
    db.execute('CREATE INDEX IF NOT EXISTS events_by_pair ON events(a,b)');db.commit()
    predicates=[('1','기업 권리자만인 공동특허','1=1'),
                ('2','기업 쌍으로 분리','1=1'),
                ('3','양쪽 모두 출원 전 5년 단독특허 존재','c.bg_a>0 AND c.bg_b>0'),
                ('4','같은 공동특허에 미국 등록특허 인용 존재','c.bg_a>0 AND c.bg_b>0 AND bw.pid IS NOT NULL'),
                ('5','출원 후 관찰기간 모두 경과','c.bg_a>0 AND c.bg_b>0 AND bw.pid IS NOT NULL AND c.deadline<=:cutoff'),
                ('6','이후 단독특허의 인용 사건 존재','c.bg_a>0 AND c.bg_b>0 AND bw.pid IS NOT NULL AND c.deadline<=:cutoff AND EXISTS(SELECT 1 FROM events e WHERE e.pid=c.pid AND e.a=c.a AND e.b=c.b)')]
    funnel=[]
    for step,label,condition in predicates:
        base=' FROM cp c LEFT JOIN backward bw ON bw.pid=c.pid WHERE '+condition
        nco=db.execute('SELECT COUNT(DISTINCT c.pid)'+base,{'cutoff':args.cutoff}).fetchone()[0]
        nlinks=db.execute('SELECT COUNT(*)'+base,{'cutoff':args.cutoff}).fetchone()[0]
        npairs=db.execute('SELECT COUNT(*) FROM (SELECT c.a,c.b'+base+' GROUP BY c.a,c.b)',{'cutoff':args.cutoff}).fetchone()[0]
        funnel.append([step,label,nco,nlinks,npairs])
    header=['step','condition','unique_copatents','copatent_pair_links','unique_pairs']
    write_csv(out/'funnel_counts.csv',header,funnel)
    entities={firm:(aid,name,category) for firm,aid,name,category in db.execute('SELECT * FROM entities')}
    pair_rows=[];cohorts=collections.defaultdict(lambda:collections.Counter())
    query='''SELECT c.a,c.b,pf.first_date,COUNT(*),
    SUM(c.bg_a>0 AND c.bg_b>0),
    SUM(c.bg_a>0 AND c.bg_b>0 AND bw.pid IS NOT NULL),
    SUM(c.bg_a>0 AND c.bg_b>0 AND bw.pid IS NOT NULL AND c.deadline<=?),
    SUM(EXISTS(SELECT 1 FROM events e WHERE e.pid=c.pid AND e.a=c.a AND e.b=c.b))
    FROM cp c LEFT JOIN backward bw ON bw.pid=c.pid LEFT JOIN pair_first pf ON pf.a=c.a AND pf.b=c.b GROUP BY c.a,c.b'''
    ev_by_pair={}
    for a,b,firm,n,ap,ex in db.execute('SELECT a,b,firm,SUM(n),SUM(n_applicant),SUM(n_examiner) FROM events GROUP BY a,b,firm'):
        ev_by_pair[(a,b,firm)]=(n,ap,ex)
    for a,b,first,n2,n3,n4,n5,n6 in db.execute(query,(args.cutoff,)):
        ea=ev_by_pair.get((a,b,a),(0,0,0)); eb=ev_by_pair.get((a,b,b),(0,0,0))
        pair_rows.append([entities[a][0],entities[a][1],entities[b][0],entities[b][1],first,n2,n3,n4,n5,n6,*ea,*eb])
        key=(first or '')[:4]
        for s,n in [(2,n2),(3,n3),(4,n4),(5,n5),(6,n6)]:
            if n:cohorts[key][s]+=1
    write_csv(out/'pair_counts.csv',['firm_A_id','firm_A_name','firm_B_id','firm_B_name','first_observed_joint_filing',
        'co_patents_stage2','co_patents_stage3','co_patents_stage4','co_patents_stage5','co_patents_stage6',
        'A_citation_events','A_applicant_events','A_examiner_events','B_citation_events','B_applicant_events','B_examiner_events'],pair_rows)
    write_csv(out/'first_collaboration_year_counts.csv',['first_observed_year','stage2_pairs','stage3_pairs','stage4_pairs','stage5_pairs','stage6_pairs'],
              [[year,*[counter[s] for s in [2,3,4,5,6]]] for year,counter in sorted(cohorts.items())])
    category_counts=dict(db.execute('SELECT category,COUNT(*) FROM entities GROUP BY category'))
    latest_grant=db.execute('SELECT MAX(granted) FROM patents WHERE granted<=?',(args.cutoff,)).fetchone()[0]
    missing_dates=db.execute('SELECT COUNT(*) FROM patents WHERE filed IS NULL').fetchone()[0]
    source_warnings=[]
    if latest_grant and (dt.date.fromisoformat(args.cutoff)-dt.date.fromisoformat(latest_grant)).days>14:
        source_warnings.append('원본의 마지막 등록일이 선언한 기준일보다 14일 넘게 이릅니다. 원본 수록 범위를 확인하세요.')
    summary={'settings':{k:getattr(args,k) for k in ['cohort_start','cohort_end','history_start','cutoff','background_years','follow_years','utility_only']},
        'unit':'Co-patents and unordered pairs of disambiguated assignee IDs; names classified by heuristic.',
        'reference_scope':'US granted-patent citation edges, all citation categories. Duplicate citing/cited edges counted once.',
        'notes':['Final Depth/Scope/Y values not computed.','No arbitrary period 1/2 split; first observed collaboration year exported.',
            'First observed date uses all available valid joint-patent history before outcome filters, including third parties.',
            'Category classification is provisional. Same-company duplicate IDs and subsidiaries not consolidated.',
            'Counts differ from the earlier 83,863 applicant-name count because source, identity and dates differ.',
            'Mature calendar windows do not remove grant/publication lags or citations not yet observed.',
            'Pairs with no events remain in pair_counts.csv; no event does not by itself establish that Y is defined.'],
        'latest_grant_date_in_source':latest_grant,'patents_without_valid_application_date':missing_dates,
        'entity_category_counts':category_counts,'funnel':[dict(zip(header,row)) for row in funnel],
        'stage5_pairs_without_stage6_event':funnel[4][4]-funnel[5][4],
        'citation_events':db.execute('SELECT COALESCE(SUM(n),0) FROM events').fetchone()[0],
        'source_warnings':source_warnings}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    definitions='''기본 조건: 공동출원 2016~2020년, 각 공동특허 출원 전 5년, 출원 후 5년. 실제 실행 조건은 summary.json을 확인하세요.
한국어 설명: 기업명 규칙으로 1차 분류한 권리자 ID 기준 표본 흐름입니다. 공동특허 1건이 3개 기업이면 기업 쌍 연결은 3건입니다.
고유 특허 수, 특허-쌍 연결 수, 고유 쌍 수를 혼동하지 마세요. 3~6번은 같은 공동특허에 누적 조건을 적용합니다.
권리자 수 1개인 특허만 배경·사후 단독특허로 사용합니다. 분류 실패 기관은 기업으로 강제 분류하지 않습니다.
인용은 출원인/심사관 등 전체를 포함하며 중복 citing/cited 연결은 하나로 셉니다.
pair_counts.csv는 인용 사건이 없는 쌍도 유지합니다. 기업 이름이 같아도 ID가 다르면 별도 기업으로 집계합니다.
first_collaboration_year_counts.csv의 연도는 제공 원자료에서의 최초 관측 공동출원연도이며 실제 최초 협력을 보장하지 않습니다.
선행·사후 조건은 달력의 고정 블록이 아니라 공동특허별 이동 창입니다. 디자인 특허 포함 여부는 summary.json을 확인하세요.
이 결과는 표본 가용성 확인용이며 Depth/Scope와 최종 Y 라벨은 아직 계산하지 않았습니다.
'''
    (out/'definitions.txt').write_text(definitions,encoding='utf-8')
    log('7/7: 결과 저장')
    with zipfile.ZipFile(out/'result_summary.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name in ['summary.json','funnel_counts.csv','pair_counts.csv','first_collaboration_year_counts.csv','definitions.txt']:
            z.write(out/name,name)
    print('\n단계 | 고유 공동특허 | 고유 기업 쌍',flush=True)
    for step,label,nco,nlinks,npairs in funnel:print(f'{step}. {label}: {nco:,}건 / {npairs:,}쌍',flush=True)
    for warning in source_warnings:log('자료 확인: '+warning)
    log('완료: '+str((out/'result_summary.zip').resolve()))
    log('result_summary.zip만 채팅에 올리세요. working.sqlite는 올리지 마세요.')
    return summary


def run(args,files,out):
    out.mkdir(parents=True,exist_ok=True)
    signature={'settings':{k:getattr(args,k) for k in ['cohort_start','cohort_end','history_start','cutoff','background_years','follow_years','utility_only']},
               'files':{k:{'path':str(p),'bytes':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for k,p in files.items()},
               'code_version':1}
    identity=out/'input_settings.json'
    if identity.exists() and json.loads(identity.read_text(encoding='utf-8'))!=signature:
        raise ValueError('조건/원본이 이전 실행과 다릅니다. --output으로 새 폴더를 지정하세요.')
    identity.write_text(json.dumps(signature,ensure_ascii=False,indent=2),encoding='utf-8')
    db=sqlite3.connect(out/'working.sqlite')
    try:
        setup(db);load_patents(db,files);load_owners(db,files);build_candidates(db,args);load_citations(db,files)
        return export(db,args,out)
    finally:db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    for opt in ['patent','application','assignee','citation']:parser.add_argument('--'+opt)
    parser.add_argument('--output',default='patent_funnel_results_2016_2020_5y')
    parser.add_argument('--cohort-start',default='2016-01-01');parser.add_argument('--cohort-end',default='2020-12-31')
    parser.add_argument('--history-start',default='2010-01-01');parser.add_argument('--cutoff',default='2025-12-31')
    parser.add_argument('--background-years',default=5,type=int);parser.add_argument('--follow-years',default=5,type=int)
    parser.add_argument('--utility-only',action='store_true')
    args=parser.parse_args()
    for key in ['cohort_start','cohort_end','history_start','cutoff']:
        if valid_date(getattr(args,key))!=getattr(args,key):raise ValueError(key+': YYYY-MM-DD 형식을 사용하세요.')
    if args.background_years<1 or args.follow_years<1:raise ValueError('기간은 1년 이상이어야 합니다.')
    if not args.history_start<=args.cohort_start<=args.cohort_end<=args.cutoff:raise ValueError('기간 순서를 확인하세요.')
    specs=[('patent','g_patent'),('application','g_application'),('assignee','g_assignee_disambiguated'),('citation','g_us_patent_citation')]
    files={stem:choose(getattr(args,opt),stem) for opt,stem in specs}
    log('공동출원 '+args.cohort_start+'~'+args.cohort_end+' / 배경 '+str(args.background_years)+'년 / 사후 '+str(args.follow_years)+'년 / 기준일 '+args.cutoff)
    run(args,files,Path(args.output).resolve())


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('\n중단했습니다. 같은 명령을 다시 실행하면 완료된 단계를 재사용합니다.',file=sys.stderr);sys.exit(130)
    except Exception as exc:
        print('\n오류: '+str(exc),file=sys.stderr);sys.exit(1)
