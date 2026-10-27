"""
쇠퇴 상위기술(subclass) 내부 세부기술(main group)의 경로 갈림(A3형) 및
convergence-lifecycle 시차 분석 파이프라인 — 원본 zip부터 전 과정 재현 가능

=== 실행 순서 (터미널에서, 이 파일과 같은 디렉토리에서) ===

  0) USPTO PatentsView에서 g_cpc_current.tsv.zip 다운로드해서 이 스크립트와
     같은 폴더에 둔다 (또는 아래 RAW_ZIP 경로를 맞게 수정).
     출처: https://patentsview.org/download/data-download-tables 의 g_cpc_current

  1) 압축 해제 + 필요 컬럼만 추출 + patent_id로 정렬 (터미널에서 직접 실행,
     파이썬으로 하면 매우 느리므로 셸 명령 사용 — 대략 10-20분, 디스크 5GB+ 필요):

       unzip -o g_cpc_current_tsv.zip                     # g_cpc_current.tsv 생성 (약 3.4GB)
       awk -F'\t' 'NR>1 {gsub(/"/,""); print $1"\t"$5"\t"$6}' g_cpc_current.tsv > slim.tsv
       rm g_cpc_current.tsv                                # 원본 삭제해 디스크 확보
       LC_ALL=C sort -t "$(printf '\t')" -k1,1 -S 1G -T . slim.tsv > sorted.tsv
       rm slim.tsv

     결과: sorted.tsv (patent_id 정렬된 patent_id / cpc_subclass / cpc_group 3열, 탭구분)

  2) 이 파이썬 스크립트를 실행: python eda_pipeline.py
     STEP 1~4가 순서대로 돌면서 data/*.pkl 을 전부 생성함 (약 5-10분 소요, 메모리 2GB+ 권장)

=== 시대 구간(bin) 정의 ===
특허번호를 시대 프록시로 사용 (근사치 — 정확히 하려면 g_application.tsv의
출원일과 조인해야 함, 아직 미실시. 이 근사가 갖는 문제: 소급 재분류 —
g_cpc_current는 '현재' CPC 분류를 과거 특허에도 소급 적용하므로, 예를 들어
2014년에 신설된 subclass가 2011년 이전 특허에도 붙어 있을 수 있음).

  b0: 특허번호 6-7백만 (대략 1999-2006)
  b1: 7-8백만
  b2: 8-9백만
  b3: 9-10백만
  b4: 10-11백만
  b5: 11-12백만
  b6: 12백만+  (부분구간 — 아직 특허 발간이 진행 중인 최신 구간이라 대부분의
                분석에서 제외하거나 결과 관측용으로만 사용)
"""

import os
import pickle
from collections import defaultdict, Counter
import statistics as st

DATA_DIR = 'data'
RAW_ZIP = 'g_cpc_current_tsv.zip'          # 원본 다운로드 파일명 (필요시 경로 수정)
SORTED_TSV = f'{DATA_DIR}/sorted.tsv'
NB = 7  # bin 개수 (b0~b6)


# =====================================================================
# STEP 0. 원본 zip -> 압축해제 -> 3컬럼 추출 -> patent_id 정렬
#   순수 파이썬으로도 가능하지만 6천만 행 정렬은 셸 sort가 훨씬 빠름.
#   이 함수는 편의상 셸 명령을 그대로 호출한다 (subprocess 사용).
#   시간이 오래 걸리므로(수 GB I/O) 이미 sorted.tsv가 있으면 건너뛴다.
# =====================================================================
def step0_build_sorted_tsv(raw_zip=RAW_ZIP, force=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SORTED_TSV) and not force:
        print(f'{SORTED_TSV} 이미 존재 — 건너뜀 (force=True로 재생성 가능)')
        return

    import subprocess
    raw_tsv = f'{DATA_DIR}/g_cpc_current.tsv'
    slim_tsv = f'{DATA_DIR}/slim.tsv'

    print('1/4 압축 해제 중... (수 분 소요)')
    subprocess.run(['unzip', '-o', '-q', raw_zip, '-d', DATA_DIR], check=True)

    print('2/4 필요 컬럼(patent_id, cpc_subclass, cpc_group)만 추출 중...')
    # g_cpc_current.tsv 컬럼 순서: patent_id, cpc_sequence, cpc_section,
    #                             cpc_class, cpc_subclass, cpc_group, cpc_type
    # -> 1번째(patent_id), 5번째(cpc_subclass), 6번째(cpc_group) 컬럼만 추출
    awk_cmd = (
        r'''awk -F'\t' 'NR>1 {gsub(/"/,""); print $1"\t"$5"\t"$6}' '''
        f'{raw_tsv} > {slim_tsv}'
    )
    subprocess.run(awk_cmd, shell=True, check=True)
    os.remove(raw_tsv)  # 3.4GB 원본은 더 이상 필요 없으므로 디스크 확보를 위해 삭제

    print('3/4 patent_id 기준 정렬 중... (몇 분 소요)')
    sort_cmd = (
        f'LC_ALL=C sort -t "$(printf \'\\t\')" -k1,1 -S 1G -T {DATA_DIR} '
        f'{slim_tsv} > {SORTED_TSV}'
    )
    subprocess.run(sort_cmd, shell=True, check=True)
    os.remove(slim_tsv)

    print('4/4 완료:', SORTED_TSV)


def bin_of(patent_id_int):
    n = patent_id_int
    if n < 6_000_000: return -1
    if n < 7_000_000: return 0
    if n < 8_000_000: return 1
    if n < 9_000_000: return 2
    if n < 10_000_000: return 3
    if n < 11_000_000: return 4
    if n < 12_000_000: return 5
    return 6


def is_indexing_maingroup(mg):
    """CPC 2000번대 main group은 색인용 보조코드라 제외"""
    num = mg[4:]
    return num.isdigit() and len(num) >= 4 and int(num) >= 2000


# =====================================================================
# STEP 1. 레벨별 cross-section 융합쌍 카운트 + 최초 등장 시대(era) 집계
#   - subclass쌍, maingroup쌍 각각에 대해 (누적 빈도, 최초 등장 era) 저장
#   - Y섹션은 제외 (2차 태그라 이중계산 위험 — CPC Y-section 문헌 검토 결과)
# =====================================================================
def step1_pair_counts():
    def era_of(pid):
        if not pid.isdigit():
            return None
        return bin_of(int(pid))

    sub_pairs = {}   # "A|B" -> (count<<2)|min_era   (era: 0=~2011 이전, ... 3=2018+)
    mg_pairs = {}
    n_pat = 0
    n_xsec_pat = 0
    capped = 0

    def era_bucket(b):
        # 원래 분석에서는 era를 4단계(0~3)로 뭉뚱그려 썼음: b0-1->0, b2->1, b3->2, b4+->3
        if b <= 1: return 0
        if b == 2: return 1
        if b == 3: return 2
        return 3

    cur = None
    subs = mgs = None
    cur_era_bucket = None

    def flush():
        nonlocal n_xsec_pat
        if len({s[0] for s in subs}) < 2:
            return
        touched = False
        sl = sorted(subs)
        for i in range(len(sl)):
            for j in range(i + 1, len(sl)):
                a, b = sl[i], sl[j]
                if a[0] == b[0]:
                    continue
                touched = True
                k = a[1] + '|' + b[1]
                v = sub_pairs.get(k)
                if v is None:
                    sub_pairs[k] = (1 << 2) | cur_era_bucket
                else:
                    sub_pairs[k] = (((v >> 2) + 1) << 2) | min(v & 3, cur_era_bucket)
        ml = sorted(mgs)
        if len(ml) <= 60:
            for i in range(len(ml)):
                for j in range(i + 1, len(ml)):
                    a, b = ml[i], ml[j]
                    if a[0] == b[0]:
                        continue
                    k = a[1] + '|' + b[1]
                    v = mg_pairs.get(k)
                    if v is None:
                        mg_pairs[k] = (1 << 2) | cur_era_bucket
                    else:
                        mg_pairs[k] = (((v >> 2) + 1) << 2) | min(v & 3, cur_era_bucket)
        else:
            nonlocal capped
            capped += 1
        if touched:
            n_xsec_pat += 1

    with open(SORTED_TSV) as f:
        for line in f:
            pid, sub, grp = line.rstrip('\n').split('\t')
            if pid != cur:
                if cur is not None and cur_era_bucket is not None and subs:
                    flush()
                n_pat_local = None
                cur = pid
                eb = era_of(pid)
                cur_era_bucket = era_bucket(eb) if eb is not None and eb >= 0 else None
                subs, mgs = set(), set()
            if cur_era_bucket is None:
                continue
            sec = sub[0]
            if sec == 'Y':
                continue
            subs.add((sec, sub))
            mgs.add((sec, grp.split('/')[0]))
        if cur is not None and cur_era_bucket is not None and subs:
            flush()

    pickle.dump(sub_pairs, open(f'{DATA_DIR}/sub_pairs.pkl', 'wb'))
    pickle.dump(mg_pairs, open(f'{DATA_DIR}/mg_pairs.pkl', 'wb'))
    return sub_pairs, mg_pairs


# =====================================================================
# STEP 2. 구간(bin, b0~b6)별 subclass/maingroup 볼륨 + cross-section 융합
#   인스턴스 수 + '2018년 이후 신규 파트너' 융합 인스턴스 수 집계
# =====================================================================
def step2_bin_series(mg_pairs):
    sub_vol = defaultdict(lambda: [0] * NB)
    mg_vol = defaultdict(lambda: [0] * NB)
    mg_conv = defaultdict(lambda: [0] * NB)
    mg_newconv = defaultdict(lambda: [0] * NB)

    cur = None
    b = None
    subs = mgs = None

    def flush():
        for s in subs:
            sub_vol[s][b] += 1
        for m in mgs:
            mg_vol[m][b] += 1
        ml = sorted(mgs)
        if len(ml) <= 60:
            for i in range(len(ml)):
                for j in range(i + 1, len(ml)):
                    a, c = ml[i], ml[j]
                    if a[0] == c[0]:
                        continue
                    k = a + '|' + c
                    v = mg_pairs.get(k)
                    if v is None:
                        continue
                    mg_conv[a][b] += 1
                    mg_conv[c][b] += 1
                    if (v & 3) == 3:  # era_bucket 3 = 2018년 이후 최초 등장
                        mg_newconv[a][b] += 1
                        mg_newconv[c][b] += 1

    with open(SORTED_TSV) as f:
        for line in f:
            pid, sub, grp = line.rstrip('\n').split('\t')
            if pid != cur:
                if cur is not None and b is not None and b >= 0 and subs:
                    flush()
                cur = pid
                b = bin_of(int(pid))
                subs, mgs = set(), set()
            if b is None or b < 0:
                continue
            if sub[0] == 'Y':
                continue
            mg = grp.split('/')[0]
            if is_indexing_maingroup(mg):
                continue
            subs.add(sub)
            mgs.add(mg)
        if cur is not None and b is not None and b >= 0 and subs:
            flush()

    for name, d in [('sub_vol', sub_vol), ('mg_vol', mg_vol),
                     ('mg_conv', mg_conv), ('mg_newconv', mg_newconv)]:
        pickle.dump(dict(d), open(f'{DATA_DIR}/{name}.pkl', 'wb'))
    return dict(sub_vol), dict(mg_vol), dict(mg_conv), dict(mg_newconv)


# =====================================================================
# STEP 3. 쌍(pair)의 '최초 등장 bin' (out-of-sample 결과 측정용,
#   STEP 1의 era_bucket보다 세밀한 bin 단위)
# =====================================================================
def step3_pair_first_bin():
    pair_first = {}
    cur = None
    b = None
    mgs = None

    def flush():
        ml = sorted(mgs)
        if len(ml) > 60:
            return
        for i in range(len(ml)):
            for j in range(i + 1, len(ml)):
                a, c = ml[i], ml[j]
                if a[0] == c[0]:
                    continue
                k = a + '|' + c
                v = pair_first.get(k)
                if v is None or b < v:
                    pair_first[k] = b

    with open(SORTED_TSV) as f:
        for line in f:
            pid, sub, grp = line.rstrip('\n').split('\t')
            if pid != cur:
                if cur is not None and b is not None and b >= 0 and mgs:
                    flush()
                cur = pid
                b = bin_of(int(pid))
                mgs = set()
            if b is None or b < 0:
                continue
            if sub[0] == 'Y':
                continue
            mg = grp.split('/')[0]
            if is_indexing_maingroup(mg):
                continue
            mgs.add(mg)
        if cur is not None and b is not None and b >= 0 and mgs:
            flush()

    pickle.dump(pair_first, open(f'{DATA_DIR}/pair_first.pkl', 'wb'))
    return pair_first


# =====================================================================
# STEP 4. 쇠퇴 subclass 판정 + A3형(경로 갈림) main group 판정
#   판정 시점: 2021년(특허 1100만, bin index 4) 까지의 정보만 사용
#   기준(전부 자의적 — robustness check 필요):
#     쇠퇴 subclass: 정점이 b0~b2, (b3+b4)/2 < 0.6*정점, 정점 볼륨 >= 500
#     A3형 maingroup: 자기 볼륨은 감소/정체 (정점 b0~b2, (b3+b4)/2 <= 0.8*정점, 정점>=50)
#                     AND 건당 융합강도가 [과거(b1+b2)->최근(b3+b4)] 로
#                         "전체 평균 배율의 1.5배"를 초과해서 상승
#                     AND 최근(b3+b4) 융합 인스턴스 수 >= 20
# =====================================================================
def step4_a3_classification(sub_vol, mg_vol, mg_conv):
    Z = [0] * NB

    tv = [0] * NB
    tc = [0] * NB
    for m, v in mg_vol.items():
        c = mg_conv.get(m, Z)
        for bb in range(NB):
            tv[bb] += v[bb]
            tc[bb] += c[bb]
    base = ((tc[3] + tc[4]) / (tv[3] + tv[4])) / ((tc[1] + tc[2]) / (tv[1] + tv[2]))

    declining = []
    for s, v in sub_vol.items():
        peak = max(v[:5])
        if peak < 500:
            continue
        if v.index(peak) <= 2 and (v[3] + v[4]) / 2 < 0.6 * peak:
            declining.append((s, v))

    A3, CTRL = [], []
    for s, sv in declining:
        for m in [m for m in mg_vol if m[:4] == s]:
            v = mg_vol[m]
            peak = max(v[:5])
            if peak < 50:
                continue
            pi = v.index(peak)
            if not (pi <= 2 and (v[3] + v[4]) / 2 <= 0.8 * peak):
                continue
            c = mg_conv.get(m, Z)
            pv, rv = v[1] + v[2], v[3] + v[4]
            if pv == 0 or rv == 0:
                continue
            p_i = (c[1] + c[2]) / pv
            r_i = (c[3] + c[4]) / rv
            if (c[3] + c[4]) >= 20 and p_i > 0 and r_i > 1.5 * base * p_i:
                A3.append((s, m, sv, v, c))
            elif p_i > 0 and r_i <= 1.0 * base * p_i:
                CTRL.append(m)

    return A3, CTRL, declining, base


# =====================================================================
# STEP 5. Out-of-sample 검증: 2021년까지 정보로 판정한 A3형/대조군이,
#   2021년 이후(b5+b6) 실제로 더 높은 신규 융합 발생률을 보였는가
#   (Mann-Whitney U, 건당 신규파트너율 A3 vs 대조군)
# =====================================================================
def step5_outcome_test(A3, CTRL, mg_vol, pair_first):
    new_partners = defaultdict(int)
    for k, fb in pair_first.items():
        if fb >= 5:  # b5(2021+) 이후 처음 생긴 쌍
            a, c = k.split('|')
            new_partners[a] += 1
            new_partners[c] += 1

    def outcome(names):
        res = []
        for m in names:
            v = mg_vol[m]
            out_vol = v[5] + v[6]
            npn = new_partners.get(m, 0)
            res.append((m, npn, out_vol, npn / out_vol if out_vol > 0 else None))
        return res

    a3_names = [m for _, m, _, _, _ in A3]
    oa = outcome(a3_names)
    oc = outcome(CTRL)

    try:
        from scipy.stats import mannwhitneyu
        ra = [x[3] for x in oa if x[3] is not None]
        rc = [x[3] for x in oc if x[3] is not None]
        _, p_rate = mannwhitneyu(ra, rc, alternative='greater')
        na = [x[1] for x in oa]
        nc = [x[1] for x in oc]
        _, p_count = mannwhitneyu(na, nc, alternative='greater')
    except ImportError:
        p_rate = p_count = None

    return oa, oc, p_rate, p_count


# =====================================================================
# STEP 6. 시차(lag) 분석: A3형 세부기술에서
#   "상위 subclass 볼륨 쇠퇴 시작 bin" vs "세부기술 융합강도 상승 시작 bin"
#   의 선후관계. lag = 쇠퇴시작bin - 융합상승시작bin
#     lag > 0 : 융합강도 상승이 쇠퇴보다 먼저 (Coccia식 선행지표 서사 지지)
#     lag = 0 : 동시
#     lag < 0 : 쇠퇴가 먼저, 융합강도 상승은 그 뒤 (집계 은폐/사후 재조명 서사 지지)
#   ** 임계값(90%, 1.2배)은 자의적 — robustness check 필요 **
# =====================================================================
def decline_start_bin(v, threshold=0.9):
    peak = max(v[:5])
    pi = v.index(peak)
    for b in range(pi + 1, 5):
        if v[b] < threshold * peak:
            return b
    return None


def conv_rise_start_bin(v, c, mult=1.2):
    intensity = [c[b] / v[b] if v[b] > 0 else 0 for b in range(NB)]
    baseline = st.mean(intensity[0:2]) if (intensity[0] + intensity[1]) > 0 else None
    if baseline is None or baseline == 0:
        for b in range(2, 6):
            if intensity[b] > 0:
                return b
        return None
    for b in range(2, 6):
        if intensity[b] > mult * baseline:
            return b
    return None


def step6_lag_analysis(A3, decline_threshold=0.9, rise_mult=1.2):
    lags, detail = [], []
    for s, m, sv, v, c in A3:
        dsb = decline_start_bin(sv, decline_threshold)
        crb = conv_rise_start_bin(v, c, rise_mult)
        if dsb is not None and crb is not None:
            lag = dsb - crb
            lags.append(lag)
            detail.append((s, m, dsb, crb, lag))
    return lags, detail


if __name__ == '__main__':
    # ---- 처음부터 전체 재현하는 경우 (원본 zip 필요) ----
    # zip 파일이 이 스크립트와 같은 폴더에 있어야 함. 이미 sorted.tsv가 있으면
    # step0은 자동으로 건너뜀.
    step0_build_sorted_tsv()                                  # zip -> sorted.tsv

    if not os.path.exists(f'{DATA_DIR}/mg_pairs.pkl'):
        sub_pairs, mg_pairs = step1_pair_counts()
    else:
        sub_pairs = pickle.load(open(f'{DATA_DIR}/sub_pairs.pkl', 'rb'))
        mg_pairs = pickle.load(open(f'{DATA_DIR}/mg_pairs.pkl', 'rb'))

    if not os.path.exists(f'{DATA_DIR}/mg_vol.pkl'):
        sub_vol, mg_vol, mg_conv, mg_newconv = step2_bin_series(mg_pairs)
    else:
        sub_vol = pickle.load(open(f'{DATA_DIR}/sub_vol.pkl', 'rb'))
        mg_vol = pickle.load(open(f'{DATA_DIR}/mg_vol.pkl', 'rb'))
        mg_conv = pickle.load(open(f'{DATA_DIR}/mg_conv.pkl', 'rb'))
        mg_newconv = pickle.load(open(f'{DATA_DIR}/mg_newconv.pkl', 'rb'))

    if not os.path.exists(f'{DATA_DIR}/pair_first.pkl'):
        pair_first = step3_pair_first_bin()
    else:
        pair_first = pickle.load(open(f'{DATA_DIR}/pair_first.pkl', 'rb'))

    # ---- 여기서부터는 이전과 동일: 판정 + 검증 + 시차분석 ----
    A3, CTRL, declining, base = step4_a3_classification(sub_vol, mg_vol, mg_conv)
    print(f'쇠퇴 subclass {len(declining)}개, A3형 {len(A3)}개, 대조군 {len(CTRL)}개')

    oa, oc, p_rate, p_count = step5_outcome_test(A3, CTRL, mg_vol, pair_first)
    print(f'out-of-sample: 건당 신규파트너율 p={p_rate}, 신규파트너 수 p={p_count}')

    lags, detail = step6_lag_analysis(A3)
    n_lead = sum(1 for l in lags if l > 0)
    n_same = sum(1 for l in lags if l == 0)
    n_lag = sum(1 for l in lags if l < 0)
    print(f'시차: 선행 {n_lead} / 동시 {n_same} / 쇠퇴 이후 {n_lag} (총 {len(lags)}개)')

    # === Park이 다음에 할 일: robustness check ===
    # for thr in [0.85, 0.9, 0.95]:
    #     for mult in [1.1, 1.2, 1.3, 1.5]:
    #         lags, _ = step6_lag_analysis(A3, thr, mult)
    #         ...분포가 얼마나 바뀌는지 확인...
