#!/usr/bin/env python3
"""현대자동차·기아의 특허 라벨 테스트용 데이터 추출.

Python 3 표준 라이브러리만 사용합니다. 추가 패키지 설치는 필요 없습니다.
이 스크립트와 아래 원본 파일을 같은 폴더에 놓고 실행하세요.
  python extract_patent_sample.py
필요 파일: g_assignee_disambiguated.tsv(.zip), g_application.tsv(.zip),
           g_us_patent_citation.tsv(.zip)
다른 위치의 파일은 선택창 또는 --assignee / --application / --citation으로 지정합니다.
원본 파일은 읽기만 합니다. 최종 라벨을 계산하는 코드가 아니라 추출 코드입니다.
기업 이름 매칭은 아래 NAME_GROUPS의 표기를 사용합니다.
"""

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

NAME_GROUPS = {
    'Hyundai Motor': ['Hyundai Motor Company', 'Hyundai Motor Co., Ltd.',
                      'Hyundai Motor Corporation', 'Hyundai Motor Corp.'],
    'Kia': ['Kia Motors Corporation', 'Kia Corporation', 'Kia Motors Corp.',
            'Kia Corp.', 'Kia Motors Company'],
}


def normal(name):
    return re.sub(r'[^A-Z0-9]', '', name.upper())


@contextmanager
def table(path, member_name):
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist()
                       if Path(n).name == member_name]
            if len(members) != 1:
                raise ValueError(f'{path.name}: {member_name} 파일을 하나 찾을 수 없습니다.')
            with archive.open(members[0]) as binary:
                with io.TextIOWrapper(binary, encoding='utf-8-sig', newline='') as f:
                    yield csv.reader(f, delimiter='\t')
    else:
        with path.open(encoding='utf-8-sig', newline='') as f:
            yield csv.reader(f, delimiter='\t')


def choose(supplied, stem, label):
    if supplied:
        path = Path(supplied).expanduser().resolve()
    else:
        candidates = set()
        for folder in {Path.cwd(), Path(__file__).resolve().parent}:
            candidates.update(p.resolve() for p in folder.glob(stem + '*')
                              if p.is_file() and p.suffix.lower() in {'.zip', '.tsv'})
        if len(candidates) == 1:
            path = candidates.pop()
        else:
            selected = ''
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                selected = filedialog.askopenfilename(title=label)
                root.destroy()
            except Exception:
                selected = input(f'{label} 경로: ').strip().strip('"')
            if not selected:
                raise ValueError(f'{label}: 파일을 선택하지 않았습니다.')
            path = Path(selected).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def copy_subset(source, member, selected_ids, archive):
    scanned = kept = 0
    with table(source, member) as reader:
        header = next(reader)
        pos = header.index('patent_id')
        with archive.open(member, 'w', force_zip64=True) as binary:
            with io.TextIOWrapper(binary, encoding='utf-8', newline='') as out:
                writer = csv.writer(out, delimiter='\t', lineterminator='\n')
                writer.writerow(header)
                for row in reader:
                    scanned += 1
                    if row[pos] in selected_ids:
                        writer.writerow(row)
                        kept += 1
                    if scanned % 5_000_000 == 0:
                        print(f'  {member}: {scanned:,}행 읽음 / {kept:,}행 추출', flush=True)
    print(f'  {member}: {scanned:,}행 중 {kept:,}행 추출', flush=True)
    return {'scanned_rows': scanned, 'kept_rows': kept}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--assignee')
    parser.add_argument('--application')
    parser.add_argument('--citation')
    parser.add_argument('--output', help='새 ZIP 파일 경로; 기존 파일은 덮어쓰지 않습니다.')
    args = parser.parse_args()
    assignee = choose(args.assignee, 'g_assignee_disambiguated', '권리자 파일 선택')
    application = choose(args.application, 'g_application', '출원일 파일 선택')
    citation = choose(args.citation, 'g_us_patent_citation', '인용 파일 선택')
    default_name = 'patent_label_sample_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.zip'
    output = Path(args.output).expanduser().resolve() if args.output else Path.cwd() / default_name
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.with_name(output.name + '.partial').exists():
        raise FileExistsError('출력 파일 또는 .partial 파일이 이미 있습니다. 다른 --output 경로를 사용하세요.')

    match = {normal(name): group for group, names in NAME_GROUPS.items() for name in names}
    selected_ids = set()
    found = {group: {} for group in NAME_GROUPS}
    print('1/5: 테스트 기업의 권리자 ID 확인', flush=True)
    with table(assignee, 'g_assignee_disambiguated.tsv') as reader:
        header = next(reader)
        pi, ni, ai = (header.index(k) for k in
                      ['patent_id', 'disambig_assignee_organization', 'assignee_id'])
        for row in reader:
            group = match.get(normal(row[ni]))
            if group:
                selected_ids.add(row[pi])
                found[group][row[ai]] = row[ni]
    if any(not values for values in found.values()):
        raise ValueError('기업 중 하나를 찾지 못했습니다. NAME_GROUPS의 이름 표기를 확인하세요.')
    firm_ids = {aid for values in found.values() for aid in values}
    print('2/5: 확인한 권리자 ID의 전체 특허번호 수집', flush=True)
    with table(assignee, 'g_assignee_disambiguated.tsv') as reader:
        header = next(reader)
        pi, ai = (header.index(k) for k in ['patent_id', 'assignee_id'])
        for row in reader:
            if row[ai] in firm_ids:
                selected_ids.add(row[pi])
    print(f'  기업 관련 특허 {len(selected_ids):,}건', flush=True)
    print(json.dumps(found, ensure_ascii=False, indent=2), flush=True)

    report = {'purpose': 'Subset for testing Depth and directional collaboration labels; labels not yet computed.',
              'matching': 'Resolve IDs with normalized exact NAME_GROUPS names, then keep all patents of those IDs; no ultimate-parent consolidation.',
              'matched_assignee_ids_and_names': found,
              'selected_patent_count': len(selected_ids),
              'citation_filter': 'Keep a row iff its citing patent_id is in selected_patent_ids. Do NOT filter citation_patent_id.',
              'period_filter': 'None. Keep all years to preserve background and subsequent patent windows.',
              'files': {}}
    # 'x' prevents overwriting an existing file. A failure leaves only a named .partial file.
    partial = output.with_name(output.name + '.partial')
    with zipfile.ZipFile(partial, mode='x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as out:
        print('3/5: 선택 특허의 모든 권리자 보존 (다른 공동권리자도 포함)', flush=True)
        report['files']['g_assignee_disambiguated.tsv'] = copy_subset(
            assignee, 'g_assignee_disambiguated.tsv', selected_ids, out)
        print('4/5: 출원일 추출', flush=True)
        report['files']['g_application.tsv'] = copy_subset(
            application, 'g_application.tsv', selected_ids, out)
        print('5/5: 인용 추출 (한 행씩 읽으므로 전체 파일을 메모리에 올리지 않음)', flush=True)
        report['files']['g_us_patent_citation.tsv'] = copy_subset(
            citation, 'g_us_patent_citation.tsv', selected_ids, out)
        out.writestr('sample_manifest.json', json.dumps(report, ensure_ascii=False, indent=2))
    if output.exists():
        raise FileExistsError(f'출력 파일이 이미 있습니다. 결과는 {partial}에 보존했습니다.')
    partial.rename(output)
    print(f'완료: {output}\n크기: {output.stat().st_size / (1024**2):.1f} MiB', flush=True)
    print('이 ZIP 파일을 채팅에 올려주세요. 원본 파일은 변경하지 않았습니다.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, StopIteration) as error:
        print(f'오류: {error}', file=sys.stderr)
        sys.exit(1)
