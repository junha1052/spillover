#!/usr/bin/env python3
"""기존 3,815쌍에서 단독특허 간 양방향 인용 집계. Python 3.9+, 추가 설치 없음.

실행: python count_cross_firm_citations.py
이 파일을 기존 count_patent_funnel.py와 같은 폴더에 놓으세요.
기존 결과 폴더의 working.sqlite와 원본 g_us_patent_citation.tsv(.zip)를 읽습니다.
필요하면 선택창에서 고릅니다. 원본·기존 DB는 변경하지 않습니다.

조건:
 * 기존 funnel의 5단계 통과 쌍 전체(예상 3,815쌍). 사후 인용 있는 609쌍만 고르지 않음.
 * 기준일은 기존 pair_first의 최초 관측 공동특허 출원일(2016년 이전일 수 있음).
 * A→B는 A의 단독특허가 B의 단독특허를 인용하는 방향.
 * 양쪽 모두 권리자 ID가 1개인 단독특허만 사용. 공동특허는 인용/피인용 양쪽에서 제외.
 * 인용한 특허의 출원일이 최초 공동특허 출원일보다 늦어야 함(같은 날 제외).
 * 기간은 출원연도 차이 0,1,2년 / 3,4,5년. 정확한 경과일 기준 구간과 다름.
   예: 최초 공동출원 2020-07-01 → 첫 구간 (2020-07-01,2022-12-31],
       뒤 구간 [2023-01-01,2025-12-31]. 각 구간 종료일까지 기준일이 지나야 완전관찰.
 * B의 단독특허는 최초 공동출원 이전/이후를 모두 포함. 시점별 민감도 집계도 함께 출력.
 * 인용 시점은 인용한 특허의 출원일로 대리. 심사 중 추가된 인용의 실제 날짜가 아님.
 * 미국 등록특허 간 인용, 모든 인용 주체 포함. 동일 citing/cited 연결은 한 번만 셈.
 * B 특허 출원일이 A 특허 출원일보다 늦은 연결도 원문대로 보존하며 별도 수를 출력.
 * 초기 배경분석의 solos 테이블(2010년부터) 대신 전체 stats/owners/patents에서
   관련 기업의 단독특허를 복원하여 오래된 첫 협력의 관찰을 임의로 0 처리하지 않음.
 * 동일 기업의 여러 권리자 ID·계열사 통합은 하지 않음. 기존 기업 분류와 표본을 유지.

주 결과는 인용 연결 수(citing patent, cited patent)와 이를 보인 기업 쌍 수입니다.
별도로 인용한 고유 단독특허 수와 단독특허 1건당 인용 연결 수도 제공합니다.
크기 차이는 단순 수치 차이이며 통계적 유의성을 뜻하지 않습니다.
결과의 cross_citation_summary.zip만 채팅에 올리세요.
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


def log(message):
    print('['+dt.datetime.now().strftime('%H:%M:%S')+'] '+message, flush=True)


def select_file(title):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root=tk.Tk();root.withdraw()
        value=filedialog.askopenfilename(title=title);root.destroy()
    except Exception:
        value=input(title+' 경로: ').strip().strip('"')
    if not value:raise ValueError('파일 선택을 취소했습니다.')
    return Path(value).expanduser().resolve()


@contextmanager
def citation_rows(path):
    if path.suffix.lower()=='.zip':
        with zipfile.ZipFile(path) as z:
            names=[n for n in z.namelist() if Path(n).name=='g_us_patent_citation.tsv']
            if len(names)!=1:raise ValueError('인용 파일에는 g_us_patent_citation.tsv가 있어야 합니다.')
            with z.open(names[0]) as b,io.TextIOWrapper(b,encoding='utf-8-sig',newline='') as f:
                yield csv.reader(f,delimiter='\t')
    else:
        with path.open(encoding='utf-8-sig',newline='') as f:yield csv.reader(f,delimiter='\t')


def bounds(first, window):
    if window==0:
        return first.toordinal()+1,dt.date(first.year+2,12,31).toordinal()
    return dt.date(first.year+3,1,1).toordinal(),dt.date(first.year+5,12,31).toordinal()


def window_for(first, ordinal):
    if ordinal<=first.toordinal():return None
    gap=dt.date.fromordinal(ordinal).year-first.year
    return 0 if 0<=gap<=2 else 1 if 3<=gap<=5 else None


def asymmetry(a,b):
    if a==0 and b==0:return 'both_zero'
    if a>0 and b==0:return 'A_only'
    if b>0 and a==0:return 'B_only'
    if a==b:return 'equal_positive'
    return 'both_positive_unequal'


def write_csv(path,header,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(header);writer.writerows(rows)


def population(db,cutoff):
    required={'cp','patents','stats','owners','entities','pair_first','backward','done'}
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required<=tables:raise ValueError('기존 funnel의 working.sqlite가 아닙니다. 누락: '+str(required-tables))
    if not db.execute("SELECT 1 FROM done WHERE name='citations'").fetchone():
        raise ValueError('기존 funnel의 인용 단계가 아직 완료되지 않았습니다.')
    query='''SELECT DISTINCT c.a,c.b,pf.first_date FROM cp c
    JOIN backward bw ON bw.pid=c.pid LEFT JOIN pair_first pf ON pf.a=c.a AND pf.b=c.b
    WHERE c.bg_a>0 AND c.bg_b>0 AND c.deadline<=? ORDER BY c.a,c.b'''
    pairs=[]
    for a,b,first in db.execute(query,(cutoff,)):
        if not first:raise ValueError('최초 공동출원일이 없는 쌍이 있습니다.')
        pairs.append((a,b,dt.date.fromisoformat(first)))
    return pairs


def load_solos(db,pairs):
    firms={f for a,b,first in pairs for f in (a,b)}
    db.execute('CREATE TEMP TABLE selected_firms(firm INTEGER PRIMARY KEY)')
    db.executemany('INSERT INTO selected_firms VALUES(?)',[(f,) for f in firms])
    # stats already enforces valid dates, grant cutoff, withdrawal and patent-type setting.
    query='''SELECT o.pid,o.firm,p.filed FROM owners o
    JOIN selected_firms sf ON sf.firm=o.firm JOIN stats s ON s.pid=o.pid
    JOIN patents p ON p.pid=o.pid WHERE s.n=1 AND s.n_company=1'''
    solo={};by_firm=collections.defaultdict(list)
    for pid,firm,filed in db.execute(query):
        ordinal=dt.date.fromisoformat(filed).toordinal()
        solo[pid]=(firm,ordinal);by_firm[firm].append(ordinal)
    for values in by_firm.values():values.sort()
    return solo,by_firm


def scan_edges(work,citation,pairs,solo):
    if work.execute("SELECT 1 FROM done WHERE name='scan'").fetchone():
        log('완료된 인용 읽기를 재사용합니다.');return
    work.execute('DELETE FROM edges');work.commit()
    lookup={(a,b):i for i,(a,b,first) in enumerate(pairs)}
    possible={}
    for a,b,first in pairs:
        lo=first.toordinal();hi=bounds(first,1)[1]
        for firm in (a,b):
            old=possible.get(firm)
            possible[firm]=(min(lo,old[0]),max(hi,old[1])) if old else (lo,hi)
    batch=[];kept=0
    sql='''INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(citing,cited)
    DO UPDATE SET applicant=MAX(applicant,excluded.applicant),examiner=MAX(examiner,excluded.examiner)'''
    with citation_rows(citation) as reader:
        header=next(reader);ci,ri=header.index('patent_id'),header.index('citation_patent_id')
        ki=header.index('citation_category') if 'citation_category' in header else None
        for n,row in enumerate(reader,1):
            source=solo.get(row[ci])
            if source:
                firm,ordinal=source;lo,hi=possible[firm]
                if lo<ordinal<=hi:
                    target=solo.get(row[ri])
                    if target and firm!=target[0]:
                        other=target[0];a,b=sorted((firm,other));index=lookup.get((a,b))
                        if index is not None:
                            first=pairs[index][2];window=window_for(first,ordinal)
                            if window is not None:
                                category=row[ki].lower() if ki is not None else ''
                                batch.append((row[ci],row[ri],index,window,0 if firm==a else 1,
                                    int('applicant' in category),int('examiner' in category),
                                    int(target[1]<first.toordinal()),int(target[1]>ordinal)))
                                kept+=1
            if len(batch)>=20000:work.executemany(sql,batch);batch.clear()
            if n%5000000==0:work.commit();log(f'인용 {n:,}행 읽음 / 관련 인용 행 {kept:,}건')
    work.executemany(sql,batch)
    work.execute("INSERT INTO done VALUES('scan')");work.commit()


def export(work,pairs,by_firm,entities,cutoff,out,settings,solo_count):
    # Each aggregate is unique citing/cited edges; distinct citing patents also reported.
    stats={}
    for index,window,direction,n,nsource,ap,ex,pre,after in work.execute('''SELECT pair_index,window,direction,
    COUNT(*),COUNT(DISTINCT citing),SUM(applicant),SUM(examiner),SUM(target_before_first),SUM(target_after_citer)
    FROM edges GROUP BY pair_index,window,direction'''):
        stats[(index,window,direction)]=(n,nsource,ap,ex,pre,after)
    categories={0:collections.Counter(),1:collections.Counter(),'all':collections.Counter()}
    norm={0:collections.Counter(),1:collections.Counter()}
    primary=[]; combined=[];per_window={};zero=(0,0,0,0,0,0)
    cutoff_ordinal=dt.date.fromisoformat(cutoff).toordinal()
    def denominator(firm,lo,hi):
        dates=by_firm.get(firm,[])
        return bisect.bisect_right(dates,hi)-bisect.bisect_left(dates,lo)
    for index,(a,b,first) in enumerate(pairs):
        totals=[0,0]
        for window in (0,1):
            lo,hi=bounds(first,window);complete=hi<=cutoff_ordinal
            ea=stats.get((index,window,0),zero);eb=stats.get((index,window,1),zero)
            na,nb=denominator(a,lo,hi),denominator(b,lo,hi)
            ra=ea[0]/na if na else None;rb=eb[0]/nb if nb else None
            klass=asymmetry(ea[0],eb[0])
            if complete:
                categories[window][klass]+=1
                categories[window]['complete_pairs']+=1
                categories[window]['A_greater']+=ea[0]>eb[0]
                categories[window]['B_greater']+=eb[0]>ea[0]
                categories[window]['both_have_solo_patents']+=na>0 and nb>0
                if na and nb:
                    norm[window]['comparable_pairs']+=1
                    norm[window]['unequal']+=ea[0]*nb!=eb[0]*na
                    norm[window]['A_greater']+=ea[0]*nb>eb[0]*na
                    norm[window]['B_greater']+=eb[0]*na>ea[0]*nb
                    norm[window]['equal']+=ea[0]*nb==eb[0]*na
            totals[0]+=ea[0];totals[1]+=eb[0]
            per_window[(index,window)]=(ea[0],eb[0],complete)
            primary.append([entities[a][0],entities[a][1],entities[b][0],entities[b][1],first.isoformat(),
                '0-2' if window==0 else '3-5',dt.date.fromordinal(lo).isoformat(),dt.date.fromordinal(hi).isoformat(),
                complete,na,nb,ea[0],eb[0],ea[1],eb[1],ra,rb,ea[0]-eb[0],
                abs(ea[0]-eb[0])/(ea[0]+eb[0]) if ea[0]+eb[0] else None,klass,
                ea[2],eb[2],ea[3],eb[3],ea[4],eb[4],ea[5],eb[5]])
        all_complete=bounds(first,1)[1]<=cutoff_ordinal
        klass=asymmetry(*totals)
        if all_complete:
            categories['all'][klass]+=1;categories['all']['complete_pairs']+=1
            categories['all']['A_greater']+=totals[0]>totals[1]
            categories['all']['B_greater']+=totals[1]>totals[0]
        combined.append([entities[a][0],entities[a][1],entities[b][0],entities[b][1],first.isoformat(),all_complete,*totals,klass])
    write_csv(out/'cross_citations_by_pair_window.csv',[
        'firm_A_id','firm_A_name','firm_B_id','firm_B_name','first_observed_joint_filing','year_gap_window',
        'window_start_inclusive','window_end_inclusive','window_complete_by_cutoff','A_solo_patents','B_solo_patents',
        'A_cites_B_links','B_cites_A_links','A_unique_citing_patents','B_unique_citing_patents',
        'A_links_per_solo_patent','B_links_per_solo_patent','A_minus_B_links','absolute_asymmetry_index','raw_count_class',
        'A_applicant_links','B_applicant_links','A_examiner_links','B_examiner_links',
        'A_links_to_B_patents_before_first_joint','B_links_to_A_patents_before_first_joint',
        'A_links_target_filed_after_source','B_links_target_filed_after_source'],primary)
    write_csv(out/'cross_citations_by_pair_all.csv',['firm_A_id','firm_A_name','firm_B_id','firm_B_name',
        'first_observed_joint_filing','full_window_complete','A_cites_B_links','B_cites_A_links','raw_count_class'],combined)
    summary_rows=[];window_summaries={}
    for window,label in [(0,'0-2'),(1,'3-5'),('all','0-5')]:
        c=categories[window]
        with_events=c['complete_pairs']-c['both_zero']
        unequal=c['A_only']+c['B_only']+c['both_positive_unequal']
        details={k:c[k] for k in ['complete_pairs','both_zero','A_only','B_only','equal_positive','both_positive_unequal','A_greater','B_greater']}
        details.update(pairs_with_any_citation=with_events,pairs_with_unequal_counts=unequal,
                       pairs_with_one_direction_only=c['A_only']+c['B_only'])
        if window!='all':
            details['pairs_with_both_firms_solo_patents']=c['both_have_solo_patents']
            details['normalized_links_per_patent_comparison']=dict(norm[window])
        window_summaries[label]=details
        summary_rows.append([label,c['complete_pairs'],with_events,c['both_zero'],c['A_only']+c['B_only'],
                             c['both_positive_unequal'],c['equal_positive'],unequal])
    write_csv(out/'window_summary.csv',['year_gap_window','complete_pairs','pairs_with_citation','both_zero_pairs',
        'one_direction_only_pairs','both_positive_unequal_pairs','equal_positive_pairs','unequal_count_pairs'],summary_rows)
    transition=collections.Counter()
    for index in range(len(pairs)):
        a0,b0,c0=per_window[(index,0)];a1,b1,c1=per_window[(index,1)]
        if c0 and c1:transition[(asymmetry(a0,b0),asymmetry(a1,b1))]+=1
    write_csv(out/'window_transition_counts.csv',['class_0_2','class_3_5','pairs'],
              [[a,b,n] for (a,b),n in sorted(transition.items())])
    temporal_after=work.execute('SELECT COUNT(*) FROM edges WHERE target_after_citer=1').fetchone()[0]
    summary={'population_pairs':len(pairs),'cutoff':cutoff,'original_funnel_settings':settings,
        'anchor':'First observed joint patent filing date from original pair_first; not first eligible 2016-2020 co-patent.',
        'direction':'A->B = A sole patent cites B sole patent. Citation direction, opposite to a knowledge-flow interpretation.',
        'windows':'Calendar filing-year differences 0,1,2 versus 3,4,5; citing filing date strictly after first joint filing date.',
        'patents':'Both endpoints have exactly one assignee ID in original stats. Relevant firms, all available filing years.',
        'target_patent_timing':'Before and after first joint filing allowed; before-first counts provided separately.',
        'time_proxy':'Citing patent filing date; not actual date a reference was added during examination.',
        'edge_count':'Distinct (citing patent, cited patent), not raw rows or number of citing patents.',
        'normalization':'Directional edge count divided by citing firm sole patent count within same window; denominator zero -> undefined.',
        'asymmetry':'One-direction-only or both-positive-unequal counts; no statistical significance test.',
        'relevant_solo_patent_count':solo_count,
        'unique_citation_edges_total':work.execute('SELECT COUNT(*) FROM edges').fetchone()[0],
        'edges_whose_cited_filing_postdates_citing_filing':temporal_after,
        'window_results':window_summaries,
        'limitations':['Assignee-name heuristic and unresolved duplicate-company IDs inherited from original funnel.',
            'Only available granted patents; earliest recorded joint filing need not be real first collaboration.',
            'Calendar maturity does not remove grant/publication lag or missing historical documents.',
            'Both-zero citation counts are distinct from having no sole patent output; denominators exported.']}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    with zipfile.ZipFile(out/'cross_citation_summary.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name in ['summary.json','window_summary.csv','cross_citations_by_pair_window.csv',
                     'cross_citations_by_pair_all.csv','window_transition_counts.csv']:z.write(out/name,name)
    print('\n기간 | 관찰 가능한 쌍 | 인용 존재 | 양방향 건수 차이',flush=True)
    for row in summary_rows:print(f'{row[0]}년: 전체 {row[1]:,}쌍 / 인용 있음 {row[2]:,}쌍 / 양방향 건수 다름 {row[7]:,}쌍',flush=True)
    log('완료: '+str((out/'cross_citation_summary.zip').resolve()))
    return summary


def run(database,citation,out,expected_pairs=3815):
    database=database.resolve();citation=citation.resolve()
    if not database.is_file() or not citation.is_file():raise FileNotFoundError('DB 또는 인용 파일 경로를 확인하세요.')
    config=database.parent/'input_settings.json'
    if not config.is_file():raise ValueError('working.sqlite와 같은 폴더에 input_settings.json이 필요합니다.')
    original=json.loads(config.read_text(encoding='utf-8'));settings=original['settings'];cutoff=settings['cutoff']
    original_bytes=original.get('files',{}).get('g_us_patent_citation',{}).get('bytes')
    if original_bytes is not None and citation.stat().st_size!=original_bytes:
        raise ValueError('인용 파일 크기가 이전 분석의 원본과 다릅니다. 이전에 사용한 동일 파일을 선택하세요.')
    out.mkdir(parents=True,exist_ok=True)
    signature={'database':str(database),'database_bytes':database.stat().st_size,'database_mtime':database.stat().st_mtime_ns,
               'citation':str(citation),'citation_bytes':citation.stat().st_size,'citation_mtime':citation.stat().st_mtime_ns,
               'cutoff':cutoff,'version':1}
    identity=out/'input_settings.json'
    if identity.exists() and json.loads(identity.read_text(encoding='utf-8'))!=signature:
        raise ValueError('이전 실행과 원본이 다릅니다. --output으로 새 폴더를 지정하세요.')
    identity.write_text(json.dumps(signature,ensure_ascii=False,indent=2),encoding='utf-8')
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as source:
        source.execute('PRAGMA cache_size=-65536');source.execute('PRAGMA temp_store=FILE')
        pairs=population(source,cutoff)
        if expected_pairs and len(pairs)!=expected_pairs:
            raise ValueError(f'대상 쌍이 {len(pairs):,}쌍입니다. 기존 3,815쌍 결과의 DB인지 확인하세요. 별도 모집단은 --expected-pairs로 지정하세요.')
        log(f'대상 기업 쌍 {len(pairs):,}쌍 / 단독특허 목록 복원')
        solo,by_firm=load_solos(source,pairs)
        entities={firm:(aid,name) for firm,aid,name in source.execute('SELECT firm,aid,name FROM entities')}
        log(f'관련 기업 단독특허 {len(solo):,}건 / 원본 인용 파일 읽기')
        with sqlite3.connect(out/'cross_citation_work.sqlite') as work:
            work.executescript('''PRAGMA synchronous=NORMAL; PRAGMA cache_size=-32768;
            CREATE TABLE IF NOT EXISTS done(name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS edges(citing TEXT,cited TEXT,pair_index INTEGER,window INTEGER,direction INTEGER,
                applicant INTEGER,examiner INTEGER,target_before_first INTEGER,target_after_citer INTEGER,
                PRIMARY KEY(citing,cited)) WITHOUT ROWID;''')
            scan_edges(work,citation,pairs,solo)
            return export(work,pairs,by_firm,entities,cutoff,out,settings,len(solo))


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--database');parser.add_argument('--citation')
    parser.add_argument('--output',default='cross_firm_citation_results');parser.add_argument('--expected-pairs',type=int,default=3815)
    args=parser.parse_args();database=Path(args.database).expanduser() if args.database else None
    if database and database.is_dir():database=database/'working.sqlite'
    if database is None:
        for folder in [Path.cwd(),Path(__file__).resolve().parent]:
            candidate=folder/'patent_funnel_results_2016_2020_5y'/'working.sqlite'
            if candidate.is_file():database=candidate;break
    if database is None:database=select_file('이전 분석의 working.sqlite 선택')
    citation=Path(args.citation).expanduser() if args.citation else None
    config=database.parent/'input_settings.json'
    if citation is None and config.is_file():
        previous=json.loads(config.read_text(encoding='utf-8')).get('files',{}).get('g_us_patent_citation',{}).get('path')
        if previous and Path(previous).is_file():citation=Path(previous)
    if citation is None:citation=select_file('원본 g_us_patent_citation.tsv.zip 또는 .tsv 선택')
    run(database,citation,Path(args.output).resolve(),args.expected_pairs)


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('\n중단했습니다. 같은 명령으로 재실행할 수 있습니다. 인용 읽기 중 중단이면 해당 단계는 처음부터 읽습니다.',file=sys.stderr);sys.exit(130)
    except Exception as exc:
        print('\n오류: '+str(exc),file=sys.stderr);sys.exit(1)
