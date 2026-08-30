import streamlit as st
import pandas as pd
import json
import re
from google import genai
from google.genai import types
from supabase import create_client, Client
from postgrest.exceptions import APIError

# 1. 환경 변수 설정
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]

# 2. 클라이언트 초기화
genai_client = genai.Client(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 스트림릿 페이지 설정
st.set_page_config(page_title="AI 뉴스 검색기", layout="wide")
st.title("📰 AI 최신 뉴스 검색 & 자동 저장기")

tabs = st.tabs(["🔍 검색하기", "💾 저장된 뉴스 보기", "📊 통계 분석"])

# --- Tab 1: 검색 및 저장 ---
with tabs[0]:
    keyword = st.text_input(
        "검색하고 싶은 뉴스 키워드를 입력하세요:",
        placeholder="예: 삼성전자 주가, 생성형 AI 트렌드"
    )
    search_button = st.button("AI 뉴스 검색 및 저장")

    if search_button and keyword:
        with st.spinner("AI가 뉴스를 검색하고 요약 중입니다..."):
            try:
                # Gemini API 호출
                prompt = f"""
                키워드 '{keyword}'에 대한 가장 최신 뉴스 2건을 검색해.
                반드시 아래 키를 가진 JSON 배열만 반환해.
                title, source, news_date, url, summary
                URL을 지어내지 마.
                """

                response = genai_client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(
                            google_search=types.GoogleSearchRetrieval()
                        )],
                        temperature=0.0
                    )
                )

                # 1) 텍스트에서 JSON 배열 추출
                raw_text = response.text or ""
                json_match = re.search(r"\[.*\]", raw_text, re.DOTALL)

                if not json_match:
                    st.error("AI 응답에서 뉴스 JSON을 찾지 못했습니다. 다시 시도해주세요.")
                    st.stop()

                news_list = json.loads(json_match.group())

                if not isinstance(news_list, list):
                    st.error("AI 응답 형식이 올바르지 않습니다.")
                    st.stop()

                # 2) Gemini 응답 형식 정리
                valid_news_list = []

                for item in news_list:
                    if not isinstance(item, dict):
                        continue

                    title = item.get("title")
                    summary = item.get("summary")

                    # 제목과 요약이 있는 뉴스만 사용
                    if title and summary:
                        valid_news_list.append({
                            "title": title,
                            "source": item.get("source", "출처 미상"),
                            "news_date": item.get(
                                "news_date",
                                item.get("date", "날짜 미상")
                            ),
                            "url": item.get("url", ""),
                            "summary": summary
                        })

                news_list = valid_news_list

                if not news_list:
                    st.error("뉴스를 읽어오지 못했습니다. 다른 검색어로 다시 시도해주세요.")
                    st.stop()

                # 3) 실제 Google 검색 출처 링크 수집
                real_links = {}

                candidates = getattr(response, "candidates", None) or []
                first_candidate = candidates[0] if candidates else None
                grounding_metadata = getattr(
                    first_candidate,
                    "grounding_metadata",
                    None
                )
                grounding_chunks = getattr(
                    grounding_metadata,
                    "grounding_chunks",
                    None
                ) or []

                for chunk in grounding_chunks:
                    web = getattr(chunk, "web", None)
                    title = getattr(web, "title", None)
                    uri = getattr(web, "uri", None)

                    if (
                        title
                        and uri
                        and uri.startswith("http")
                        and "grounding-api-redirect" not in uri
                    ):
                        real_links[title] = uri

                # Gemini 제목과 실제 검색 결과 제목이 유사하면 URL 교체
                for item in news_list:
                    for real_title, real_uri in real_links.items():
                        if (
                            item["title"] in real_title
                            or real_title in item["title"]
                        ):
                            item["url"] = real_uri
                            break

                # 4) 결과 출력 및 DB 저장
                success_count = 0
                dup_count = 0

                for news in news_list:
                    with st.container():
                        if news["url"]:
                            st.markdown(
                                f"### [{news['title']}]({news['url']})"
                            )
                        else:
                            st.markdown(f"### {news['title']}")

                        st.caption(
                            f"📅 {news['news_date']} | 🏢 {news['source']}"
                        )
                        st.write(news["summary"])
                        st.divider()

                    try:
                        data = {
                            "keyword": keyword,
                            "title": news["title"],
                            "source": news["source"],
                            "news_date": news["news_date"],
                            "url": news["url"],
                            "summary": news["summary"]
                        }

                        supabase.table("news_history").insert(data).execute()
                        success_count += 1

                    except APIError as e:
                        if "23505" in str(e):
                            dup_count += 1
                        else:
                            st.error(f"DB 저장 오류: {e}")

                st.toast(
                    f"✅ 완료! (신규 저장: {success_count}건, "
                    f"중복 제외: {dup_count}건)"
                )

            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")

# --- Tab 2: 저장된 뉴스 보기 ---
with tabs[1]:
    st.subheader("저장된 뉴스 히스토리")

    res = supabase.table("news_history").select("*").order(
        "created_at",
        desc=True
    ).execute()

    if res.data:
        df = pd.DataFrame(res.data)

        search_q = st.text_input("검색어 또는 제목으로 필터링:", "")
        filtered_df = df[
            df["title"].str.contains(search_q, na=False)
            | df["keyword"].str.contains(search_q, na=False)
        ]

        st.dataframe(filtered_df, use_container_width=True)

        csv = filtered_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "CSV 결과 다운로드",
            data=csv,
            file_name="news_history.csv",
            mime="text/csv"
        )
    else:
        st.info("아직 저장된 뉴스가 없습니다.")

# --- Tab 3: 통계 분석 ---
with tabs[2]:
    st.subheader("데이터 통계")

    res = supabase.table("news_history").select(
        "keyword, created_at"
    ).execute()

    if res.data:
        df_stat = pd.DataFrame(res.data)
        col1, col2 = st.columns(2)

        with col1:
            st.write("📌 키워드별 누적 검색 건수")
            keyword_counts = df_stat["keyword"].value_counts()
            st.bar_chart(keyword_counts)

        with col2:
            st.write("📅 일자별 저장 건수")
            df_stat["date"] = pd.to_datetime(
                df_stat["created_at"]
            ).dt.date
            date_counts = df_stat.groupby("date").size()
            st.line_chart(date_counts)
    else:
        st.info("통계를 표시할 데이터가 부족합니다.")
