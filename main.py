import csv
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import pandas as pd
import streamlit as st

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

APP_VERSION = "v8.0"
APP_TITLE = "오토커머스 AI"
OUTPUT_ROOT = Path("outputs")

st.set_page_config(page_title=f"{APP_TITLE} {APP_VERSION}", page_icon="🛒", layout="wide")


def safe_filename(text: str) -> str:
    text = (text or "project").strip()
    text = re.sub(r"[^0-9a-zA-Z가-힣_\-]+", "_", text)
    return text[:60].strip("_") or "project"


def parse_coupang_url(url: str) -> dict:
    parsed = urlparse((url or "").strip())
    qs = parse_qs(parsed.query)
    product_id = ""
    m = re.search(r"/products/(\d+)", parsed.path)
    if m:
        product_id = m.group(1)
    item_id = qs.get("itemId", [""])[0]
    vendor_item_id = qs.get("vendorItemId", [""])[0]
    q = qs.get("q", [""])[0]
    keyword = unquote(q).replace("+", " ").strip()
    return {
        "원본링크": url,
        "분석상태": "성공" if product_id else "확인필요",
        "상품ID": product_id,
        "아이템ID": item_id,
        "벤더아이템ID": vendor_item_id,
        "키워드": keyword,
        "도메인": parsed.netloc,
        "경로": parsed.path,
        "링크유형": "쿠팡 상품 상세 링크" if "coupang" in parsed.netloc else "일반 링크",
    }


def guess_product_name(form_name: str, url_info: dict) -> str:
    return (form_name or url_info.get("키워드") or "이 제품").strip()


def product_profile(product_name, price_memo, pain_point, benefit, target, keyword, image_name=None):
    scenes = ["문제 상황", "제품 등장", "사용 장면", "정리 전후 비교", "클로즈업", "CTA 장면"]
    selling_points = ["편리함", "시간 절약", "공간 활용", "반복 불편 감소", "정리 스트레스 감소", "전후 차이 명확"]
    image_insight = {
        "상태": "이미지 업로드됨" if image_name else "이미지 없음",
        "추천활용": "대표컷/클로즈업/비교컷에 활용" if image_name else "제품 사진을 업로드하면 촬영 플랜을 더 구체화할 수 있음",
        "추천구도": "정면 1컷 + 손 사용 장면 1컷 + 전후 비교 1컷",
        "썸네일활용": "제품을 화면 오른쪽, 큰 자막을 왼쪽 배치",
    }
    if any(k in product_name for k in ["주방", "수납", "정리", "싱크"]):
        scenes = ["싱크대 하부 정리", "양념통/냄비 찾는 장면", "슬라이딩으로 꺼내는 장면", "정리 전후 비교", "제품 클로즈업", "댓글 CTA"]
        selling_points = ["공간 활용", "찾기 쉬움", "정리 스트레스 감소", "깔끔한 전후", "슬라이딩 사용감", "생활 동선 개선"]
    return {
        "상품명": product_name,
        "가격메모": price_memo,
        "불편함": pain_point,
        "핵심장점": benefit,
        "추천대상": target,
        "CTA키워드": keyword,
        "이미지파일": image_name or "",
        "촬영장면": scenes,
        "판매포인트": selling_points,
        "이미지분석": image_insight,
    }


def hook_matrix(product_name, pain_point, benefit, keyword):
    return {
        "공감형": f"저만 {pain_point} 때문에 귀찮았던 거 아니죠?",
        "조회수형": f"이거 모르면 {pain_point} 계속 반복됩니다",
        "비교형": "정리 전후 차이, 생각보다 큽니다",
        "스토리형": f"저도 처음엔 {product_name} 별거 아닌 줄 알았어요",
        "Before/After": f"Before는 {pain_point}, After는 훨씬 편하게",
        "리뷰형": f"직접 써보면 {benefit}이 제일 크게 느껴집니다",
        "ASMR형": f"딸깍, 꺼내는 순간 정리 스트레스 끝",
        "댓글유도형": f"댓글에 '{keyword}' 남길 준비 되셨나요?",
        "엄마시점": f"바쁜 날에도 바로 쓰기 좋은 생활템입니다",
        "반전형": f"작은 제품인데 생활 동선이 바뀝니다",
    }


def hooks(product_name, pain_point, benefit, style, keyword):
    matrix = hook_matrix(product_name, pain_point, benefit, keyword)
    base = list(matrix.values())
    if style == "하센맘 공감형":
        base[0] = f"저만 {pain_point} 때문에 귀찮았던 거 아니죠?"
        base[3] = f"솔직히 {product_name}, 왜 이제 알았지 싶었어요"
    elif style == "Before/After형":
        base[0] = matrix["Before/After"]
        base[1] = "정리 전후 차이, 영상으로 보면 더 큽니다"
    elif style == "조회수 후킹형":
        base[0] = matrix["조회수형"]
        base[1] = "댓글 반응 좋았던 생활꿀템"
    return base[:10]


def script_text(product_name, pain_point, benefit, target, keyword, style, length_sec):
    detail_line = ""
    if length_sec >= 45:
        detail_line = f"가격이나 배송 조건까지 괜찮다면, 살림템 후보로 충분히 볼 만합니다.\n"
    if style == "하센맘 공감형":
        body = f"""저만 {pain_point} 때문에 은근히 귀찮았던 거 아니죠?
저도 매번 별거 아닌데 시간이 잡아먹히더라고요.
그럴 때 쓰기 좋은 게 바로 {product_name}입니다.
복잡하게 설명할 필요 없이, 핵심은 {benefit}이라는 점이에요.
직접 사용 장면과 전후 차이를 같이 보여주면 훨씬 이해가 쉬워요.
{detail_line}제품 정보가 궁금하시면 댓글에 '{keyword}' 남겨주세요."""
    elif style == "Before/After형":
        body = f"""Before, {pain_point} 때문에 매번 답답했죠.
After, {product_name} 하나로 동선이 훨씬 편해집니다.
처음엔 작은 차이처럼 보이지만 실제로 써보면 {benefit}이 가장 크게 느껴져요.
전후 비교 장면으로 보여주면 설득력이 확 올라갑니다.
{target}이라면 한 번 확인해보세요.
{detail_line}댓글에 '{keyword}' 남겨주시면 제품 정보 확인하실 수 있어요."""
    else:
        body = f"""{product_name}, 아직도 그냥 넘기세요?
매번 {pain_point} 때문에 은근히 불편했다면 이 제품 한 번 확인해보세요.
핵심 장점은 {benefit}이라는 점입니다.
바쁜 일상에서도 바로 쓰기 좋고, 작은 불편함을 줄이는 데 도움이 됩니다.
특히 {target}이라면 더 잘 맞을 수 있어요.
{detail_line}제품 정보가 궁금하시면 댓글에 '{keyword}' 남겨주세요."""
    if length_sec <= 15:
        return "\n".join(body.splitlines()[:4]) + f"\n댓글에 '{keyword}' 남겨주세요."
    if length_sec <= 30:
        return "\n".join(body.splitlines()[:6])
    return body


def subtitles_from_script(script: str, keyword: str):
    lines = [x.strip() for x in script.splitlines() if x.strip()]
    short = []
    for line in lines:
        short.append(line[:34] + "…" if len(line) > 34 else line)
    while len(short) < 6:
        short.append(f"댓글에 '{keyword}' 남겨주세요 👇")
    return short[:8]


def thumbnails(product_name, pain_point, benefit, keyword):
    return [
        f"아직도 {pain_point}?",
        "왜 이제 알았지?",
        f"{product_name} 하나로 편하게",
        "정리 전후 차이 보세요",
        "작은 불편함 끝",
        "생활이 달라졌어요",
        "살림 난이도 내려갑니다",
        "이거 진짜 편합니다",
        f"댓글 '{keyword}'",
        "바쁜 날 필수템",
    ]


def capcut_plan(profile, keyword, length_sec):
    scenes = profile["촬영장면"]
    if length_sec <= 15:
        times = ["0~2초", "2~5초", "5~9초", "9~13초", "13~15초"]
    elif length_sec <= 30:
        times = ["0~3초", "3~7초", "7~14초", "14~24초", "24~30초"]
    else:
        times = ["0~3초", "3~8초", "8~15초", "15~25초", f"25~{length_sec}초"]
    return [
        {"시간": times[0], "장면": scenes[0], "자막": "큰 글씨, 화면 중앙 하단", "효과음": "Pop 35%", "편집": "0.3초 줌인 + 흔들림 억제", "BGM": "Clean Morning 10~18%"},
        {"시간": times[1], "장면": scenes[1], "자막": "흰색 본문 + 노란 강조", "효과음": "Click 40%", "편집": "짧은 컷 2~3개", "BGM": "밝은 생활꿀팁"},
        {"시간": times[2], "장면": scenes[2], "자막": "제품명 강조", "효과음": "Whoosh 35%", "편집": "제품 확대 + 밝기 약간 상승", "BGM": "Minimal Pop"},
        {"시간": times[3], "장면": scenes[3], "자막": "장점 2개 순서대로", "효과음": "Soft Bell 25%", "편집": "전후 비교 컷", "BGM": "Soft Piano"},
        {"시간": times[4], "장면": scenes[-1], "자막": f"댓글 '{keyword}' 크게", "효과음": "Click 35%", "편집": "키워드 바운스", "BGM": "마무리 18%"},
    ]


def storyboard(profile, hook, subtitles):
    return pd.DataFrame([
        {"컷": 1, "목적": "시선 잡기", "촬영": profile["촬영장면"][0], "자막": hook},
        {"컷": 2, "목적": "공감 형성", "촬영": profile["촬영장면"][1], "자막": subtitles[1]},
        {"컷": 3, "목적": "제품 소개", "촬영": profile["촬영장면"][2], "자막": subtitles[2]},
        {"컷": 4, "목적": "장점 설명", "촬영": profile["촬영장면"][3], "자막": subtitles[3]},
        {"컷": 5, "목적": "댓글 전환", "촬영": profile["촬영장면"][-1], "자막": subtitles[-1]},
    ])


def production_checklist(profile):
    return {
        "촬영 준비": ["제품 외관 닦기", "배경 정리", "정리 전 상태 일부 남겨두기", "세로 9:16 고정"],
        "필수 컷": profile["촬영장면"],
        "CapCut 설정": ["본문 글씨 38~44", "강조색 #FFD54F", "자막 위치 하단 40%", "그림자 50~60%", "효과음 볼륨 25~40%"],
        "업로드 전 확인": ["쿠팡파트너스 고지", "댓글 CTA", "링크 위치 확인", "썸네일 문구 8자 내외"],
    }


def score_report(product_name, benefit, hooks_list, script):
    hook_score = min(100, 70 + len([h for h in hooks_list if "?" in h]) * 3 + 8)
    clarity = 94 if benefit else 75
    cta = 96 if "댓글" in script else 72
    purchase = 90 if any(k in product_name for k in ["정리", "수납", "주방", "생활"]) else 84
    total = round((hook_score + clarity + cta + purchase) / 4)
    return {
        "총점": total,
        "조회수잠재력": hook_score,
        "설명명확도": clarity,
        "댓글유도력": cta,
        "구매전환가능성": purchase,
        "예상성공률": f"{total}%",
        "개선포인트": ["첫 3초에 전후 비교 컷 추가", "제품 클로즈업 1컷 추가", "댓글 키워드를 더 크게 표시", "썸네일 문구는 6~10자 추천"],
    }


def upload_package(product_name, benefit, target, keyword, scenes):
    instagram = f"""매번 반복되는 작은 불편함 때문에 불편하셨다면 이 제품 한번 확인해보세요.

✅ 제품명: {product_name}
✅ 포인트: {benefit}
✅ 추천: {target}
✅ 활용 장면: {', '.join(scenes[:3])}

팔로우 하시고 댓글에 '{keyword}' 남겨주세요 👇"""
    youtube_desc = f"""이 제품 쇼핑쇼츠 기획안입니다.

제품명: {product_name}
핵심 포인트: {benefit}
추천 대상: {target}

🔗 제품 정보는 영상 아래 설명란 링크 또는 프로필 링크를 확인해주세요.
댓글에 '{keyword}' 남겨주세요 👇"""
    return {
        "instagram": {"caption": instagram, "hashtags": ["#쇼핑쇼츠", "#생활꿀템", "#쿠팡추천", "#살림템", "#하센맘"]},
        "youtube": {"title": f"매번 반복되는 작은 불편함 해결템? {product_name} 쇼츠 리뷰", "description": youtube_desc},
        "pinned_comment": f"제품 정보 궁금하시면 댓글에 '{keyword}' 남겨주세요 👇",
    }


def make_srt(subtitles, length_sec):
    blocks = []
    sec_per = max(2, int(length_sec / max(1, len(subtitles))))
    start = 0
    for i, line in enumerate(subtitles, 1):
        end = min(length_sec, start + sec_per)
        blocks.append(f"{i}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{line}\n")
        start = end
    return "\n".join(blocks)


def maybe_gpt(api_key, payload):
    if not api_key or OpenAI is None:
        return None
    try:
        client = OpenAI(api_key=api_key)
        prompt = f"""
너는 하센맘 스타일 쇼핑쇼츠 전문 작가야. 아래 정보를 바탕으로 자연스러운 한국어 쇼핑쇼츠 패키지를 JSON으로 만들어줘.
과장광고, 의료효능 단정은 금지. 댓글 CTA 포함.
반드시 JSON만 출력.

입력정보:
{json.dumps(payload, ensure_ascii=False)}

형식:
{{"후킹":[],"대본":"","자막":[],"썸네일":[],"인스타본문":"","유튜브제목":"","유튜브설명":"","개선제안":[]}}
"""
        resp = client.chat.completions.create(model="gpt-4.1-mini", messages=[{"role":"user","content":prompt}], temperature=0.8)
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        return {"GPT오류": str(e)}


def build_package(form, url_info, uploaded_name, api_key):
    product_name = guess_product_name(form["product_name"], url_info)
    keyword = form["keyword"].strip() or (product_name.split()[0] if product_name else "제품")
    length_sec = int(form["length_sec"])
    profile = product_profile(product_name, form["price_memo"], form["pain_point"], form["benefit"], form["target"], keyword, uploaded_name)
    hs = hooks(product_name, profile["불편함"], profile["핵심장점"], form["style"], keyword)
    matrix = hook_matrix(product_name, profile["불편함"], profile["핵심장점"], keyword)
    script = script_text(product_name, profile["불편함"], profile["핵심장점"], profile["추천대상"], keyword, form["style"], length_sec)
    subs = subtitles_from_script(script, keyword)
    thumbs = thumbnails(product_name, profile["불편함"], profile["핵심장점"], keyword)
    capcut = capcut_plan(profile, keyword, length_sec)
    story = storyboard(profile, hs[0], subs)
    checklist = production_checklist(profile)
    scores = score_report(product_name, profile["핵심장점"], hs, script)
    package = upload_package(product_name, profile["핵심장점"], profile["추천대상"], keyword, profile["촬영장면"])
    gpt_result = maybe_gpt(api_key, {"상품": profile, "스타일": form["style"], "콘텐츠유형": form["content_type"], "길이": length_sec})
    improvements = ["첫 후킹은 12자 이내로 더 짧게 테스트하세요", "CTA 키워드는 썸네일과 댓글에 동일하게 쓰세요", "전후 비교 컷을 1개 이상 넣으면 구매 설득력이 올라갑니다"]
    if isinstance(gpt_result, dict) and "GPT오류" not in gpt_result:
        hs = gpt_result.get("후킹") or hs
        script = gpt_result.get("대본") or script
        subs = gpt_result.get("자막") or subtitles_from_script(script, keyword)
        thumbs = gpt_result.get("썸네일") or thumbs
        package["instagram"]["caption"] = gpt_result.get("인스타본문") or package["instagram"]["caption"]
        package["youtube"]["title"] = gpt_result.get("유튜브제목") or package["youtube"]["title"]
        package["youtube"]["description"] = gpt_result.get("유튜브설명") or package["youtube"]["description"]
        improvements = gpt_result.get("개선제안") or improvements
    return {
        "쿠팡링크분석": url_info,
        "상품분석": profile,
        "AI점수카드": scores,
        "유형별후킹매트릭스": matrix,
        "AI개선제안": improvements,
        "하센맘AI리포트": {"스타일": form["style"], "콘텐츠유형": form["content_type"], "길이": f"{length_sec}초", "핵심전략": "공감 후킹 → 문제 장면 → 제품 해결 → 전후 비교 → 댓글 CTA", "추천촬영난이도": "중간", "BGM추천": ["CapCut 검색어: 밝은 생활꿀팁", "Minimal Pop", "Clean Morning", "Soft Piano"], "SFX추천": ["Pop 35%", "Click 40%", "Whoosh 35%", "Soft Bell 25%"]},
        "후킹10개": hs[:10],
        "대본": script,
        "자막": subs,
        "썸네일10개": thumbs[:10],
        "스토리보드": story.to_dict(orient="records"),
        "CapCut편집가이드": capcut,
        "촬영체크리스트": checklist,
        "플랫폼별업로드패키지": package,
        "SRT": make_srt(subs, length_sec),
        "생성방식": "GPT" if isinstance(gpt_result, dict) and "GPT오류" not in gpt_result else "LOCAL",
    }


def package_files(data):
    files = {}
    files["result.json"] = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    files["script.txt"] = data["대본"].encode("utf-8")
    files["subtitles.srt"] = data["SRT"].encode("utf-8")
    files["instagram.txt"] = data["플랫폼별업로드패키지"]["instagram"]["caption"].encode("utf-8")
    files["youtube.txt"] = (data["플랫폼별업로드패키지"]["youtube"]["title"] + "\n\n" + data["플랫폼별업로드패키지"]["youtube"]["description"]).encode("utf-8")
    files["capcut_plan.csv"] = pd.DataFrame(data["CapCut편집가이드"]).to_csv(index=False).encode("utf-8-sig")
    files["storyboard.csv"] = pd.DataFrame(data["스토리보드"]).to_csv(index=False).encode("utf-8-sig")
    md = f"# AutoCommerceAI {APP_VERSION}\n\n## 대본\n{data['대본']}\n\n## 후킹\n" + "\n".join([f"- {x}" for x in data["후킹10개"]])
    files["summary.md"] = md.encode("utf-8")
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    bio.seek(0)
    return files, bio.getvalue()


def save_outputs(data, folder_name):
    folder = OUTPUT_ROOT / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    files, zip_bytes = package_files(data)
    for name, content in files.items():
        (folder / name).write_bytes(content)
    (folder / "shorts_package.zip").write_bytes(zip_bytes)
    return folder, files, zip_bytes


def codebox(label, text, height=180):
    st.caption(f"📄 {label} — 아래 내용을 복사해서 사용하세요")
    st.code(text, language=None)


def render_downloads(data, zip_bytes):
    files, _ = package_files(data)
    st.download_button("📦 전체 쇼츠 제작 패키지 ZIP 다운로드", zip_bytes, "shorts_package.zip", "application/zip", use_container_width=True)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.download_button("JSON", files["result.json"], "result.json", "application/json")
    with c2:
        st.download_button("대본 TXT", files["script.txt"], "script.txt", "text/plain")
    with c3:
        st.download_button("SRT", files["subtitles.srt"], "subtitles.srt", "text/plain")
    with c4:
        st.download_button("CapCut CSV", files["capcut_plan.csv"], "capcut_plan.csv", "text/csv")


with st.sidebar:
    st.markdown("## 🎬 오토커머스 AI")
    st.caption(f"쇼핑쇼츠 MVP {APP_VERSION}")
    api_key = st.text_input("OpenAI API 키", type="password")
    menu = st.radio("메뉴", ["쇼핑쇼츠 생성", "프로젝트", "생성기록", "설정", "사용 가이드"])

if menu == "사용 가이드":
    st.title(f"사용 가이드 {APP_VERSION}")
    st.write("쿠팡 링크 입력 → 상품명/불편함/장점 보완 → 생성 → 탭별 확인 → ZIP 다운로드")
elif menu in ["프로젝트", "생성기록"]:
    st.title(menu)
    OUTPUT_ROOT.mkdir(exist_ok=True)
    projects = sorted([p for p in OUTPUT_ROOT.glob("*") if p.is_dir()], reverse=True)
    if not projects:
        st.info("아직 저장된 프로젝트가 없습니다.")
    else:
        for p in projects[:30]:
            st.write(f"📁 {p.name}")
elif menu == "설정":
    st.title("설정")
    st.info("현재 버전은 로컬 저장 방식입니다. API 키는 저장하지 않고 실행 중에만 사용합니다.")
else:
    st.title(f"🛒 쇼핑쇼츠 자동 생성 {APP_VERSION}")
    st.success("목표: 쿠팡 링크 → 탭형 결과 → 원클릭 복사 → 전체 ZIP 패키지 다운로드")
    with st.form("shorts_form"):
        coupang_url = st.text_input("쿠팡 상품 링크", value="https://www.coupang.com/vp/products/8965495882?itemId=26172744561&vendorItemId=93152433461&q=%EC%A3%BC%EB%B0%A9%EC%88%98%EB%82%A9%EC%A0%95%EB%A6%AC%ED%95%A8&searchId=88876dcf7871247&sourceType=search&itemsCount=60&searchRank=1&rank=1&traceId=mqyukr3i")
        c1, c2 = st.columns(2)
        with c1:
            product_name = st.text_input("상품명", placeholder="예: 주방수납 정리함")
            price_memo = st.text_input("가격/혜택 메모", placeholder="예: 29,900원, 로켓배송")
            target = st.text_input("추천 대상", value="생활을 조금 더 편하게 만들고 싶은 분")
            content_type = st.selectbox("콘텐츠 유형", ["문제해결형", "Before/After형", "공감리뷰형", "생활꿀팁형", "랭킹형", "ASMR형", "브이로그형"])
            length_sec = st.selectbox("쇼츠 길이", [15, 30, 45, 60], index=1)
        with c2:
            pain_point = st.text_input("고객 불편함", value="매번 반복되는 작은 불편함")
            benefit = st.text_input("핵심 장점", value="사용이 간편하고 일상에 바로 도움이 됨")
            keyword = st.text_input("CTA 키워드", placeholder="예: 정리함")
            style = st.selectbox("대본 스타일", ["하센맘 공감형", "Before/After형", "조회수 후킹형", "일반 쇼핑형"])
        uploaded = st.file_uploader("상품 이미지 선택(선택)", type=["png", "jpg", "jpeg", "webp"])
        submitted = st.form_submit_button("분석하고 쇼츠 생성")
    if submitted:
        form = {"product_name": product_name, "price_memo": price_memo, "target": target, "content_type": content_type, "pain_point": pain_point, "benefit": benefit, "keyword": keyword, "style": style, "length_sec": length_sec}
        url_info = parse_coupang_url(coupang_url)
        data = build_package(form, url_info, uploaded.name if uploaded else None, api_key)
        folder_name = datetime.now().strftime("%Y%m%d_%H%M%S_") + safe_filename(data["상품분석"]["상품명"])
        folder, files, zip_bytes = save_outputs(data, folder_name)
        st.info(f"저장 위치: {folder}")

        tabs = st.tabs(["📋 기획", "📝 대본", "🎬 편집", "📥 다운로드", "🤖 AI 리포트"])
        with tabs[0]:
            st.subheader("상품 분석")
            st.json(data["상품분석"])
            st.subheader("후킹 10개")
            for x in data["후킹10개"]:
                st.write("•", x)
            st.subheader("썸네일 10개")
            for x in data["썸네일10개"]:
                st.write("•", x)
            st.subheader("유형별 후킹 매트릭스")
            st.json(data["유형별후킹매트릭스"])
        with tabs[1]:
            st.subheader("대본")
            codebox("대본 복사용", data["대본"])
            st.subheader("자막")
            codebox("자막 복사용", "\n".join(data["자막"]))
            st.subheader("스토리보드")
            st.dataframe(pd.DataFrame(data["스토리보드"]), use_container_width=True)
        with tabs[2]:
            st.subheader("CapCut 편집 가이드")
            st.dataframe(pd.DataFrame(data["CapCut편집가이드"]), use_container_width=True)
            st.subheader("촬영 체크리스트")
            st.json(data["촬영체크리스트"])
            st.subheader("인스타 릴스 본문")
            codebox("인스타 복사용", data["플랫폼별업로드패키지"]["instagram"]["caption"])
            st.subheader("유튜브 쇼츠 제목/설명")
            codebox("유튜브 제목", data["플랫폼별업로드패키지"]["youtube"]["title"])
            codebox("유튜브 설명", data["플랫폼별업로드패키지"]["youtube"]["description"])
        with tabs[3]:
            render_downloads(data, zip_bytes)
            st.success("ZIP 안에 result.json, script.txt, subtitles.srt, capcut_plan.csv, storyboard.csv, instagram.txt, youtube.txt, summary.md가 포함됩니다.")
        with tabs[4]:
            c1, c2, c3, c4 = st.columns(4)
            sr = data["AI점수카드"]
            c1.metric("총점", sr["총점"])
            c2.metric("조회수", sr["조회수잠재력"])
            c3.metric("댓글유도", sr["댓글유도력"])
            c4.metric("구매전환", sr["구매전환가능성"])
            st.subheader("AI 개선 제안")
            for x in data["AI개선제안"]:
                st.write("•", x)
            with st.expander("원본 링크 분석 보기"):
                st.json(data["쿠팡링크분석"])
            with st.expander("하센맘 AI 리포트"):
                st.json(data["하센맘AI리포트"])
