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
    keyword = st.text_input("검색하고 싶은 뉴스 키워드를 입력하세요:", placeholder="예: 삼성전자 주가, 생성형 AI 트렌드")
    search_button = st.button("AI 뉴스 검색 및 저장")

    if search_button and keyword:
        with st.spinner("AI가 뉴스를 검색하고 요약 중입니다..."):
            try:
                # Gemini API 호출 (Google 검색 도구 활용)
                prompt = f"키워드 '{keyword}'에 대한 가장 최신 뉴스 딱 2건만 검색해. 제목, 출처, 날짜, 원본 URL, 요약을 포함한 JSON 배열로 응답하고 절대 URL을 지어내지 마."
                
                response = genai_client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearchRetrieval())],
                        temperature=0.0
                    )
                )

                # 1) 텍스트에서 JSON 추출 (마크다운 제거)
                raw_text = response.text
                json_match = re.search(r'\[.*\]', raw_text, re.DOTALL)
                if not json_match:
                    st.error("AI 응답 형식이 올바르지 않습니다. 다시 시도해주세요.")
                    st.stop()
                
                news_list = json.loads(json_match.group())

                # 2) [URL 환각 방지 로직] 실제 grounding_metadata에서 실제 링크 매칭
                candidates = response.candidtes or []

                if candidates:
                    grounding_metadata = candidates[0].grounding_metadata
                    grounding_chunks = (
                        grounding_metadata.grounding_chunks
                        if grounding_metadata and grounding_metadata.grounding_chunks
                        else []
                    )
                    
                    for chunk in grounding_metadata.grounding_chunks:
                        if chunk.web:
                            title = chunk.web.title
                            uri = chunk.web.uri
                            # 리다이렉트 링크가 아니고 http로 시작하는 경우만 수집
                            if "grounding-api-redirect" not in uri and uri.startswith("http"):
                                real_links[title] = uri

                # JSON 데이터의 URL을 실제 링크로 교체
                for item in news_list:
                    for real_title, real_uri in real_links.items():
                        # 제목이 유사하거나 포함된 경우 실제 URL로 덮어쓰기
                        if item['title'] in real_title or real_title in item['title']:
                            item['url'] = real_uri
                            break

                # 3) 결과 출력 및 DB 저장
                success_count = 0
                dup_count = 0

                for news in news_list:
                    # 화면 출력
                    with st.container():
                        st.markdown(f"### [{news['title']}]({news['url']})")
                        st.caption(f"📅 {news['news_date']} | 🏢 {news['source']}")
                        st.write(news['summary'])
                        st.divider()

                    # DB 저장
                    try:
                        data = {
                            "keyword": keyword,
                            "title": news['title'],
                            "source": news['source'],
                            "news_date": news['news_date'],
                            "url": news['url'],
                            "summary": news['summary']
                        }
                        supabase.table("news_history").insert(data).execute()
                        success_count += 1
                    except APIError as e:
                        if "23505" in str(e): # 중복 키 에러 코드
                            dup_count += 1
                        else:
                            st.error(f"DB 저장 오류: {e}")

                st.toast(f"✅ 완료! (신규 저장: {success_count}건, 중복 제외: {dup_count}건)")

            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")

# --- Tab 2: 저장된 뉴스 보기 ---
with tabs[1]:
    st.subheader("저장된 뉴스 히스토리")
    
    # 데이터 불러오기
    res = supabase.table("news_history").select("*").order("created_at", desc=True).execute()
    if res.data:
        df = pd.DataFrame(res.data)
        
        # 필터링 UI
        search_q = st.text_input("검색어 또는 제목으로 필터링:", "")
        filtered_df = df[df['title'].str.contains(search_q) | df['keyword'].str.contains(search_q)]
        
        st.dataframe(filtered_df, use_container_width=True)
        
        # CSV 다운로드
        csv = filtered_df.to_csv(index=False).encode('utf-8-sig')
        st.download_button("CSV 결과 다운로드", data=csv, file_name="news_history.csv", mime="text/csv")
    else:
        st.info("아직 저장된 뉴스가 없습니다.")

# --- Tab 3: 통계 분석 ---
with tabs[2]:
    st.subheader("데이터 통계")
    res = supabase.table("news_history").select("keyword, created_at").execute()
    
    if res.data:
        df_stat = pd.DataFrame(res.data)
        col1, col2 = st.columns(2)
        
        with col1:
            st.write("📌 키워드별 누적 검색 건수")
            keyword_counts = df_stat['keyword'].value_counts()
            st.bar_chart(keyword_counts)
            
        with col2:
            st.write("📅 일자별 저장 건수")
            df_stat['date'] = pd.to_datetime(df_stat['created_at']).dt.date
            date_counts = df_stat.groupby('date').size()
            st.line_chart(date_counts)
    else:
        st.info("통계를 표시할 데이터가 부족합니다.")
