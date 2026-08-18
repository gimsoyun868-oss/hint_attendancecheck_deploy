# Streamlit Cloud redeploy: 2026-08-17
from io import BytesIO
from pathlib import Path
from datetime import date, datetime
from math import ceil
import json
import re

import altair as alt
import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from pdf_report import build_priority_pdf

st.set_page_config(
    page_title="K-뉴딜아카데미 출결 현황",
    page_icon=":material/groups:",
    layout="wide",
)

DATA_PATH = Path(__file__).parent / "data" / "participants.csv"
DAILY_PATH = Path(__file__).parent / "data" / "daily_attendance.csv"
GOOGLE_SHEET_ID = "1rVwWjo6EOdlRoqtrZ4v4d68vXbC2Pw7HQ3zaNpKIE34"
REPORT_SHEET_ID = "13rFvlyikQrFbEQBssEurh2J9PBFqyw_5TzbQi0xMy7o"
OAUTH_TOKEN_PATH = Path(__file__).parent / "google_oauth_token.json"
CACHE_SCHEMA_VERSION = "2026-08-12-risk-percent-v2"
EDITOR_EMAILS = {
    "hint.soyun@gmail.com",
    "osudongi122@gmail.com",
    "hint.kpc@gmail.com",
}
REPORT_HEADERS = [
    "작성일시",
    "기준일",
    "작성자",
    "전체 요약",
    "결석·이상 증가 원인",
    "특이사항",
    "조치 내용",
    "추후 확인사항",
]


def _authorized_user_credentials() -> dict | None:
    """Return the deployed Google credential without exposing it to callers."""
    if OAUTH_TOKEN_PATH.exists():
        try:
            return json.loads(OAUTH_TOKEN_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    try:
        return dict(st.secrets["google_authorized_user"])
    except (KeyError, StreamlitSecretNotFoundError):
        return None


@st.cache_data(ttl="1m", max_entries=2, show_spinner=False)
def load_allowed_emails(sheet_id: str, credentials: dict) -> set[str]:
    """Read email/active rows from the first worksheet of the access list."""
    import gspread
    from google.oauth2.credentials import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    client = gspread.authorize(Credentials.from_authorized_user_info(credentials, scopes=scopes))
    rows = client.open_by_key(sheet_id).sheet1.get_all_values()
    allowed: set[str] = set()
    disabled_values = {"n", "no", "false", "0", "중지", "비활성", "해제"}
    for row in rows[1:]:
        email = row[0].strip().lower() if row else ""
        active = row[1].strip().lower() if len(row) > 1 else "y"
        if email and "@" in email and active not in disabled_values:
            allowed.add(email)
    return allowed


def _report_service_account_credentials() -> dict | None:
    """Return the report writer credential stored only in Streamlit secrets."""
    try:
        return dict(st.secrets["report_service_account"])
    except (KeyError, StreamlitSecretNotFoundError):
        return None


@st.cache_data(ttl="1m", max_entries=2, show_spinner=False)
def load_daily_reports(sheet_id: str, credentials: dict) -> pd.DataFrame:
    import gspread

    client = gspread.service_account_from_dict(credentials)
    worksheet = client.open_by_key(sheet_id).sheet1
    values = worksheet.get_all_values()
    if not values or values[0] != REPORT_HEADERS:
        return pd.DataFrame(columns=REPORT_HEADERS)
    rows = worksheet.get_all_records(expected_headers=REPORT_HEADERS)
    frame = pd.DataFrame(rows, columns=REPORT_HEADERS)
    if not frame.empty:
        frame["기준일"] = pd.to_datetime(frame["기준일"], errors="coerce").dt.date
    return frame


def save_daily_report(sheet_id: str, credentials: dict, values: list[str]) -> None:
    import gspread

    client = gspread.service_account_from_dict(credentials)
    worksheet = client.open_by_key(sheet_id).sheet1
    if not worksheet.row_values(1):
        worksheet.update("A1:H1", [REPORT_HEADERS])
        worksheet.freeze(rows=1)
    worksheet.append_row(values, value_input_option="USER_ENTERED")
    load_daily_reports.clear()


@st.cache_data(ttl="10m", max_entries=3, show_spinner="교육생 현황을 불러오는 중입니다...")
def load_participants(path: str, modified_at: float, schema_version: str) -> pd.DataFrame:
    del modified_at, schema_version
    frame = pd.read_csv(path, encoding="utf-8-sig")
    frame["출석률"] = pd.to_numeric(frame["출석률"], errors="coerce").clip(lower=0, upper=1)
    frame["출석일수"] = pd.to_numeric(frame["출석일수"], errors="coerce")
    frame["지각·조퇴·외출"] = pd.to_numeric(frame["지각·조퇴·외출"], errors="coerce").fillna(0)
    if "재적상태" not in frame.columns:
        frame["재적상태"] = "재적"
    return add_early_warning(frame)


@st.cache_data(ttl="10m", max_entries=3, show_spinner="날짜별 출결을 불러오는 중입니다...")
def load_daily_attendance(path: str, modified_at: float) -> pd.DataFrame:
    del modified_at
    frame = pd.read_csv(path, encoding="utf-8-sig", parse_dates=["날짜"])
    frame["상태"] = frame["상태"].astype(str).str.strip()
    return frame


def _number(value, default=0):
    if value in (None, ""):
        return default
    cleaned = str(value).replace(",", "").replace("%", "").strip()
    try:
        number = float(cleaned)
        return number / 100 if "%" in str(value) else number
    except ValueError:
        return default


def _parse_sheet_date(value):
    if not value:
        return None
    parsed = pd.to_datetime(str(value).strip(), errors="coerce")
    if pd.isna(parsed) or parsed.year < 2000:
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", str(value).strip())
        if match:
            parsed = pd.Timestamp(datetime.now().year, int(match.group(1)), int(match.group(2)))
    return None if pd.isna(parsed) else parsed.normalize()


def _dark_name_rows(spreadsheet) -> dict[str, set[int]]:
    """Return zero-based row numbers whose name cell uses a dark fill."""
    ranges = [f"'{worksheet.title.replace(chr(39), chr(39) * 2)}'!M19:M200" for worksheet in spreadsheet.worksheets()]
    try:
        metadata = spreadsheet.fetch_sheet_metadata(
            params={
                "includeGridData": "true",
                "ranges": ranges,
                "fields": "sheets(properties(title),data(startRow,rowData(values(effectiveFormat(backgroundColor,backgroundColorStyle)))))",
            }
        )
    except Exception:
        return {}

    dark_rows: dict[str, set[int]] = {}
    for sheet in metadata.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "")
        rows: set[int] = set()
        for grid in sheet.get("data", []):
            start_row = int(grid.get("startRow", 18))
            for offset, row_data in enumerate(grid.get("rowData", [])):
                values = row_data.get("values", [])
                if not values:
                    continue
                effective_format = values[0].get("effectiveFormat", {})
                color = (
                    effective_format.get("backgroundColorStyle", {}).get("rgbColor")
                    or effective_format.get("backgroundColor", {})
                )
                red = float(color.get("red", 0))
                green = float(color.get("green", 0))
                blue = float(color.get("blue", 0))
                luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
                if color and luminance < 0.45:
                    rows.add(start_row + offset)
        if rows:
            dark_rows[title] = rows
    return dark_rows


@st.cache_data(ttl="2m", max_entries=2, show_spinner="구글 시트 최신 내용을 동기화하는 중입니다...")
def load_google_sheet(
    sheet_id: str, credentials: dict, credential_type: str = "service_account"
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    import gspread

    if credential_type == "authorized_user":
        from google.oauth2.credentials import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        client = gspread.authorize(Credentials.from_authorized_user_info(credentials, scopes=scopes))
    else:
        client = gspread.service_account_from_dict(credentials)
    spreadsheet = client.open_by_key(sheet_id)
    dropout_rows = _dark_name_rows(spreadsheet)
    people_records: list[dict] = []
    daily_records: list[dict] = []
    today = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).normalize()

    for worksheet in spreadsheet.worksheets():
        match = re.match(r"^(\d+)\.\s*(.+?)_(.+)$", worksheet.title)
        if not match:
            continue
        class_number = int(match.group(1))
        region, course = match.group(2).strip(), match.group(3).strip()
        class_name = f"{class_number}반"
        values = worksheet.get_all_values()
        if len(values) < 19:
            continue

        date_columns: list[tuple[int, pd.Timestamp]] = []
        for column, raw_date in enumerate(values[17]):
            if column < 16:
                continue
            parsed_date = _parse_sheet_date(raw_date)
            if parsed_date is not None and parsed_date <= today:
                date_columns.append((column, parsed_date))

        for row_index, row in enumerate(values[18:], start=18):
            name = row[12].strip() if len(row) > 12 else ""
            if not name or name.startswith("※"):
                break
            rate = _number(row[13] if len(row) > 13 else "", default=0)
            days = _number(row[14] if len(row) > 14 else "", default=0)
            events = _number(row[15] if len(row) > 15 else "", default=0)
            people_records.append(
                {
                    "반번호": class_number,
                    "반": class_name,
                    "권역": region,
                    "과정": course,
                    "이름": name,
                    "출석률": rate,
                    "출석일수": days,
                    "지각·조퇴·외출": events,
                    "재적상태": "퇴소" if row_index in dropout_rows.get(worksheet.title, set()) else "재적",
                    "위험구간": "80% 미만" if rate < 0.8 else "90% 미만" if rate < 0.9 else "정상",
                }
            )
            for column, date in date_columns:
                status = row[column].strip() if len(row) > column else ""
                if not status or status == "해당없음":
                    continue
                daily_records.append(
                    {
                        "반번호": class_number,
                        "반": class_name,
                        "권역": region,
                        "과정": course,
                        "이름": name,
                        "날짜": date,
                        "상태": status,
                    }
                )

    if not people_records or not daily_records:
        raise ValueError("17개 반의 교육생 출결 영역을 찾지 못했습니다.")
    people = add_early_warning(pd.DataFrame(people_records).sort_values(["반번호", "이름"]))
    daily = pd.DataFrame(daily_records).sort_values(["날짜", "반번호", "이름"])
    return people, daily, pd.Timestamp.now(tz="Asia/Seoul")


def load_dashboard_data() -> tuple[pd.DataFrame, pd.DataFrame, str, pd.Timestamp]:
    if OAUTH_TOKEN_PATH.exists():
        try:
            credentials = json.loads(OAUTH_TOKEN_PATH.read_text(encoding="utf-8"))
            people, daily, synced_at = load_google_sheet(
                GOOGLE_SHEET_ID, credentials, "authorized_user"
            )
            return people, daily, "내 Google 계정 실시간", synced_at
        except Exception as error:
            st.warning(f"내 Google 계정 동기화 실패로 다른 연결을 확인합니다: {error}", icon=":material/account_circle_off:")

    try:
        credentials = dict(st.secrets["google_authorized_user"])
        people, daily, synced_at = load_google_sheet(
            GOOGLE_SHEET_ID, credentials, "authorized_user"
        )
        return people, daily, "회사 Google 계정 실시간", synced_at
    except (KeyError, StreamlitSecretNotFoundError):
        pass
    except Exception as error:
        st.warning(f"배포용 Google 계정 동기화 실패로 다른 연결을 확인합니다: {error}", icon=":material/cloud_off:")

    try:
        credentials = dict(st.secrets["gcp_service_account"])
        people, daily, synced_at = load_google_sheet(GOOGLE_SHEET_ID, credentials)
        return people, daily, "구글 시트 실시간", synced_at
    except (KeyError, StreamlitSecretNotFoundError):
        pass
    except Exception as error:
        st.warning(f"구글 시트 동기화 실패로 저장된 데이터를 표시합니다: {error}", icon=":material/cloud_off:")

    raise RuntimeError(
        "Google Sheets 연결 정보를 찾지 못했습니다. 개인정보 보호를 위해 저장된 교육생 자료는 표시하지 않습니다."
    )


def summarize_attendance(frame: pd.DataFrame, period_column: str) -> pd.DataFrame:
    statuses = ["출석", "인정출석", "결석", "지각", "조퇴", "외출"]
    counts = (
        frame.assign(건수=1)
        .pivot_table(
            index=period_column,
            columns="상태",
            values="건수",
            aggfunc="sum",
            fill_value=0,
        )
        .reindex(columns=statuses, fill_value=0)
        .reset_index()
    )
    counts["지각·조퇴·외출"] = counts[["지각", "조퇴", "외출"]].sum(axis=1)
    counts["환산결석"] = counts["결석"] + (counts["지각·조퇴·외출"] // 3)
    counts["집계건수"] = counts[statuses].sum(axis=1)
    counts["환산출석률"] = (
        (counts["집계건수"] - counts["환산결석"]) / counts["집계건수"].replace(0, pd.NA)
    ).fillna(0).clip(0, 1)
    return counts


def add_early_warning(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    scores: list[int] = []
    levels: list[str] = []
    reasons: list[str] = []
    converted_absences: list[int] = []
    remaining_events: list[int] = []

    for _, row in result.iterrows():
        rate = float(row["출석률"]) if pd.notna(row["출석률"]) else 0.0
        events = int(row["지각·조퇴·외출"])
        converted_absence, event_remainder = divmod(events, 3)
        score = 0
        causes: list[str] = []

        if str(row.get("재적상태", "재적")) == "퇴소":
            scores.append(0)
            levels.append("퇴소")
            reasons.append("퇴소자")
            converted_absences.append(converted_absence)
            remaining_events.append(event_remainder)
            continue

        if rate < 0.70:
            score += 60
            causes.append("출석률 70% 미만")
        elif rate < 0.80:
            score += 45
            causes.append("출석률 80% 미만")
        elif rate < 0.90:
            score += 25
            causes.append("출석률 90% 미만")
        elif rate < 0.95:
            score += 10
            causes.append("출석률 95% 미만")

        if converted_absence:
            score += min(converted_absence * 20, 40)
            causes.append(f"지각·조퇴·외출 {events}회 → 결석 {converted_absence}회 환산")

        score = min(score, 100)
        if score >= 60:
            level = "고위험"
        elif score >= 35:
            level = "주의"
        elif score >= 15:
            level = "관찰"
        else:
            level = "정상"

        scores.append(score)
        levels.append(level)
        reasons.append(", ".join(causes) if causes else "특이 위험 신호 없음")
        converted_absences.append(converted_absence)
        remaining_events.append(event_remainder)

    result["환산결석"] = converted_absences
    result["환산잔여횟수"] = remaining_events
    result["위험점수"] = scores
    result["이탈위험도"] = result["위험점수"] / 100
    result["위험등급"] = levels
    result["주요 원인"] = reasons
    return result


def to_excel_bytes(frame: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="교육생 통합현황")
        sheet = writer.book["교육생 통합현황"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column_cells in sheet.columns:
            width = min(max(len(str(cell.value or "")) for cell in column_cells) + 2, 36)
            sheet.column_dimensions[column_cells[0].column_letter].width = width
    return buffer.getvalue()


def require_access() -> None:
    try:
        auth_configured = "auth" in st.secrets
        allowed_emails = {
            str(email).strip().lower()
            for email in st.secrets.get("ALLOWED_EMAILS", [])
            if str(email).strip()
        }
        allowed_emails.update(EDITOR_EMAILS)
        access_sheet_id = str(st.secrets.get("ACCESS_SHEET_ID", "")).strip()
    except StreamlitSecretNotFoundError:
        auth_configured = False
        allowed_emails = set()
        access_sheet_id = ""

    if auth_configured:
        if not getattr(st.user, "is_logged_in", False):
            st.title("K-뉴딜아카데미 출결 현황")
            st.caption("허용된 회사 Google 계정으로 로그인해야 이용할 수 있습니다.")
            if st.button("Google 계정으로 로그인", type="primary", icon=":material/login:"):
                st.login()
            st.stop()

        email = str(st.user.get("email", "")).strip().lower()
        email_verified = bool(st.user.get("email_verified", False))
        if access_sheet_id:
            credentials = _authorized_user_credentials()
            if credentials is None:
                st.error("접근 권한 목록 연결 정보를 찾지 못했습니다.", icon=":material/cloud_off:")
                st.stop()
            try:
                allowed_emails.update(load_allowed_emails(access_sheet_id, credentials))
            except Exception:
                st.error("접근 권한 목록을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.", icon=":material/cloud_off:")
                st.stop()
        if not email_verified or email not in allowed_emails:
            st.error("이 계정은 대시보드 접근 권한이 없습니다.", icon=":material/block:")
            st.caption(f"현재 로그인 계정: {email or '확인 불가'}")
            if st.button("다른 Google 계정으로 로그인", icon=":material/logout:"):
                st.logout()
            st.stop()

        with st.sidebar:
            st.caption(f"로그인: {email}")
            if st.button("로그아웃", icon=":material/logout:", width="stretch"):
                st.logout()
        st.session_state["current_user_email"] = email
        return

    try:
        password = st.secrets["APP_PASSWORD"]
    except (KeyError, StreamlitSecretNotFoundError):
        password = ""
    if not password:
        st.session_state["current_user_email"] = "local-admin"
        st.info(
            "로컬 미리보기 모드입니다. 공개 배포 환경에서는 Google 로그인이 필수입니다.",
            icon=":material/shield:",
        )
        return
    if st.session_state.get("authenticated"):
        st.session_state["current_user_email"] = "local-admin"
        return
    st.title("K-뉴딜아카데미 출결 현황")
    entered = st.text_input("접근 비밀번호", type="password")
    if st.button("로그인", type="primary", icon=":material/login:"):
        if entered == password:
            st.session_state.authenticated = True
            st.rerun()
        st.error("비밀번호가 올바르지 않습니다.")
    st.stop()


require_access()

with st.container(border=True):
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        with st.container(gap=None):
            st.title("K-뉴딜아카데미 출결 현황")
            st.caption("17개 반 500명의 출결과 이탈 위험 신호를 한 화면에서 확인합니다.")
        with st.container(horizontal=True, vertical_alignment="center"):
            st.badge("500명 통합", color="blue", icon=":material/groups:")
            st.badge("개인정보 최소화", color="green", icon=":material/shield:")

try:
    df, daily_df, data_source, synced_at = load_dashboard_data()
except Exception as error:
    st.error(str(error), icon=":material/cloud_off:")
    st.stop()
if "이탈위험도" not in df.columns:
    df = add_early_warning(df)

with st.sidebar:
    st.header("필터")
    query = st.text_input("교육생 검색", placeholder="이름을 입력하세요", icon=":material/search:")
    regions = st.multiselect("권역", sorted(df["권역"].dropna().unique()), default=[])
    classes = st.multiselect("반", df.sort_values("반번호")["반"].drop_duplicates().tolist(), default=[])
    selected_levels = st.pills(
        "위험등급",
        ["고위험", "주의", "관찰", "정상", "퇴소"],
        selection_mode="multi",
        default=[],
    )
    if st.button("지금 새로고침", icon=":material/refresh:", width="stretch"):
        load_google_sheet.clear()
        st.rerun()
    st.badge(
        data_source,
        color="green" if "실시간" in data_source else "orange",
        icon=":material/cloud_done:" if "실시간" in data_source else ":material/database:",
    )
    st.caption(f"마지막 데이터 갱신: {synced_at.strftime('%Y-%m-%d %H:%M:%S')}")
    st.caption("구글 시트 연결 시 2분마다 최신 내용을 확인합니다.")

filtered = df.copy()
if query:
    filtered = filtered[filtered["이름"].str.contains(query.strip(), case=False, na=False)]
if regions:
    filtered = filtered[filtered["권역"].isin(regions)]
if classes:
    filtered = filtered[filtered["반"].isin(classes)]
if selected_levels:
    filtered = filtered[filtered["위험등급"].isin(selected_levels)]

with st.container(horizontal=True, horizontal_alignment="distribute"):
    st.metric(
        "조회 교육생",
        f"{len(filtered):,}명",
        f"전체 {len(df):,}명 중 {len(filtered) / max(len(df), 1):.0%}",
        border=True,
        chart_data=filtered.groupby("반").size().tolist(),
        chart_type="bar",
    )
    st.metric("평균 출석률", f"{filtered['출석률'].clip(upper=1).mean():.1%}" if len(filtered) else "-", border=True)
    st.metric("고위험", f"{(filtered['위험등급'] == '고위험').sum():,}명", border=True)
    st.metric("주의·관찰", f"{filtered['위험등급'].isin(['주의', '관찰']).sum():,}명", border=True)

overview, warning, manager_view, people, classes_view, period_view = st.tabs(
    ["운영 요약", "이탈 조기경보", "우선관리·리포트", "전체 교육생", "반별 현황", "기간별 출결"],
    default="운영 요약",
)

with overview:
    overview_daily = daily_df.copy()
    if query:
        overview_daily = overview_daily[
            overview_daily["이름"].str.contains(query.strip(), case=False, na=False)
        ]
    if regions:
        overview_daily = overview_daily[overview_daily["권역"].isin(regions)]
    if classes:
        overview_daily = overview_daily[overview_daily["반"].isin(classes)]

    if overview_daily.empty:
        st.info("현재 필터에 해당하는 출결 기록이 없습니다.", icon=":material/filter_alt_off:")
    else:
        expected_people = max(overview_daily["이름"].nunique(), 1)
        daily_coverage = overview_daily.groupby("날짜")["이름"].nunique().sort_index()
        minimum_complete = max(1, int(expected_people * 0.8))
        completed_dates = daily_coverage[daily_coverage >= minimum_complete]
        if not completed_dates.empty:
            latest_date = completed_dates.index.max()
            coverage_note = "입력 완료 기준 80% 이상"
        else:
            latest_date = daily_coverage.idxmax()
            coverage_note = "입력 건수가 가장 많은 날짜"
        latest = overview_daily[overview_daily["날짜"] == latest_date].copy()
        latest_coverage = len(latest) / expected_people
        displayed_coverage = min(latest_coverage, 1.0)
        excess_records = max(len(latest) - expected_people, 0)
        latest_status = (
            latest["상태"]
            .value_counts()
            .reindex(["출석", "인정출석", "결석", "지각", "조퇴", "외출"], fill_value=0)
            .rename_axis("상태")
            .reset_index(name="인원")
        )
        latest_events = int(latest["상태"].isin(["지각", "조퇴", "외출"]).sum())
        latest_absences = int((latest["상태"] == "결석").sum())
        latest_present = int(latest["상태"].isin(["출석", "인정출석", "지각", "조퇴", "외출"]).sum())
        latest_rate = latest_present / max(len(latest), 1)

        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            with st.container(gap=None):
                st.subheader("오늘의 운영 요약")
                st.caption(
                    f"최근 집계일 {latest_date:%Y-%m-%d} · {coverage_note} · "
                    f"기준 {expected_people:,}명 · 입력 {len(latest):,}건"
                )
            with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                st.badge(
                    f"입력률 {displayed_coverage:.0%}",
                    color="green" if latest_coverage >= 0.8 else "orange",
                    icon=":material/fact_check:",
                )
                if excess_records:
                    st.badge(
                        f"신규·중복 확인 {excess_records:,}명",
                        color="orange",
                        icon=":material/person_search:",
                    )

        with st.container(horizontal=True, horizontal_alignment="distribute"):
            st.metric("당일 출석률", f"{latest_rate:.1%}", border=True)
            st.metric("결석", f"{latest_absences:,}명", border=True)
            st.metric("지각·조퇴·외출", f"{latest_events:,}명", border=True)
            st.metric("확인 필요", f"{latest_absences + latest_events:,}명", border=True)

        status_chart = (
            alt.Chart(latest_status[latest_status["인원"] > 0])
            .mark_arc(innerRadius=55, outerRadius=90)
            .encode(
                theta=alt.Theta("인원:Q"),
                color=alt.Color(
                    "상태:N",
                    scale=alt.Scale(
                        domain=["출석", "인정출석", "결석", "지각", "조퇴", "외출"],
                        range=["#1967D2", "#188038", "#B3261E", "#E37400", "#F9AB00", "#9334E6"],
                    ),
                    legend=alt.Legend(title=None, orient="bottom", columns=3),
                ),
                tooltip=["상태", alt.Tooltip("인원:Q", format="d")],
            )
            .properties(height=250)
        )

        class_daily = latest.assign(
            출석인정=latest["상태"].isin(["출석", "인정출석", "지각", "조퇴", "외출"]).astype(int),
            확인필요=latest["상태"].isin(["결석", "지각", "조퇴", "외출"]).astype(int),
        )
        class_daily = (
            class_daily.groupby(["반번호", "반", "권역", "과정"], as_index=False)
            .agg(교육생=("이름", "count"), 출석인원=("출석인정", "sum"), 확인필요=("확인필요", "sum"))
            .assign(출석률=lambda x: x["출석인원"] / x["교육생"])
            .assign(
                출석구간=lambda x: pd.cut(
                    x["출석률"],
                    bins=[-float("inf"), 0.9, 0.95, float("inf")],
                    labels=["90% 미만", "90~95%", "95% 이상"],
                    right=False,
                )
            )
            .sort_values("반번호")
        )
        class_order = class_daily["반"].tolist()
        class_chart_height = max(360, len(class_daily) * 29)
        class_bars = (
            alt.Chart(class_daily)
            .mark_bar(cornerRadiusEnd=5, size=18)
            .encode(
                y=alt.Y(
                    "반:N",
                    sort=class_order,
                    title=None,
                    axis=alt.Axis(labelLimit=90, labelPadding=8, labelOverlap=False),
                ),
                x=alt.X(
                    "출석률:Q",
                    title=None,
                    scale=alt.Scale(domain=[0, 1.08]),
                    axis=alt.Axis(format="%", values=[0, 0.25, 0.5, 0.75, 1]),
                ),
                color=alt.Color(
                    "출석구간:N",
                    scale=alt.Scale(
                        domain=["90% 미만", "90~95%", "95% 이상"],
                        range=["#B3261E", "#E37400", "#1967D2"],
                    ),
                    legend=None,
                ),
                tooltip=[
                    "반",
                    "권역",
                    "과정",
                    alt.Tooltip("교육생:Q", format="d"),
                    alt.Tooltip("출석률:Q", format=".1%"),
                    alt.Tooltip("확인필요:Q", title="확인 필요", format="d"),
                ],
            )
        )
        class_labels = (
            alt.Chart(class_daily)
            .mark_text(align="left", baseline="middle", dx=6, fontSize=12, fontWeight="bold", color="#3C4043")
            .encode(
                y=alt.Y("반:N", sort=class_order),
                x=alt.X("출석률:Q"),
                text=alt.Text("출석률:Q", format=".1%"),
            )
        )
        class_chart = (class_bars + class_labels).properties(height=class_chart_height)

        chart_left, chart_right = st.columns([0.8, 1.7], gap="medium")
        with chart_left:
            with st.container(border=True, height="stretch"):
                st.markdown("**최근 출결 구성**")
                st.altair_chart(status_chart)
        with chart_right:
            with st.container(border=True, height="stretch"):
                st.markdown("**반별 당일 출석률**")
                st.caption("반 번호 순으로 전체 17개 반을 표시합니다. 빨강은 90% 미만, 주황은 95% 미만입니다.")
                class_chart_tab, class_table_tab = st.tabs(["출석률 그래프", "반별 요약표"])
                with class_chart_tab:
                    st.altair_chart(class_chart)
                with class_table_tab:
                    st.dataframe(
                        class_daily[["반", "권역", "과정", "교육생", "출석률", "확인필요"]],
                        hide_index=True,
                        column_config={
                            "반": st.column_config.TextColumn("반", pinned=True),
                            "교육생": st.column_config.NumberColumn("교육생", format="%d명"),
                            "출석률": st.column_config.ProgressColumn(
                                "당일 출석률", format="percent", min_value=0, max_value=1
                            ),
                            "확인필요": st.column_config.NumberColumn("확인 필요", format="%d명"),
                        },
                        height=min(610, 38 + len(class_daily) * 35),
                        key="latest_class_summary_table",
                    )

        attention = latest[latest["상태"].isin(["결석", "지각", "조퇴", "외출"])][
            ["반", "권역", "과정", "이름", "상태"]
        ].sort_values(["상태", "반", "이름"])
        with st.container(border=True):
            with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                st.markdown("**당일 확인 명단**")
                st.badge(f"{len(attention)}명", color="red" if len(attention) else "green")
            if attention.empty:
                st.caption("결석·지각·조퇴·외출 기록이 없습니다.")
            else:
                st.dataframe(
                    attention,
                    hide_index=True,
                    column_config={
                        "반": st.column_config.TextColumn("반", pinned=True),
                        "이름": st.column_config.TextColumn("이름", pinned=True),
                    },
                    height=min(340, 38 + len(attention) * 35),
                    key="latest_attention_table",
                )

with warning:
    st.caption(
        ":material/info: 출결 신호를 이용한 규칙 기반 조기경보입니다. 실제 중도탈락 확률이 아니며 담당자의 확인을 돕는 우선순위 지표입니다."
    )
    risk_df = filtered[~filtered["위험등급"].isin(["정상", "퇴소"])].sort_values(
        ["위험점수", "출석률"], ascending=[False, True]
    )
    risk_counts = (
        df["위험등급"]
        .value_counts()
        .reindex(["고위험", "주의", "관찰", "정상", "퇴소"], fill_value=0)
        .rename_axis("위험등급")
        .reset_index(name="인원")
    )
    chart = (
        alt.Chart(risk_counts)
        .mark_bar(cornerRadiusEnd=4)
        .encode(
            y=alt.Y("위험등급:N", sort=["고위험", "주의", "관찰", "정상", "퇴소"], title=None),
            x=alt.X("인원:Q", title="교육생 수"),
            color=alt.Color(
                "위험등급:N",
                scale=alt.Scale(
                    domain=["고위험", "주의", "관찰", "정상", "퇴소"],
                    range=["#B3261E", "#F29900", "#F9AB00", "#188038", "#5F6368"],
                ),
                legend=None,
            ),
            tooltip=["위험등급", "인원"],
        )
        .properties(height=210)
    )
    chart_col, priority_col = st.columns([1.05, 1.95], gap="large")
    with chart_col:
        with st.container(border=True, height="stretch"):
            st.subheader("위험등급 분포", help="전체 500명의 현재 조기경보 등급입니다.")
            st.altair_chart(chart)
            with st.container(horizontal=True, horizontal_alignment="distribute"):
                st.badge(f"고위험 {int((df['위험등급'] == '고위험').sum())}명", color="red")
                st.badge(f"주의 {int((df['위험등급'] == '주의').sum())}명", color="orange")
                st.badge(f"관찰 {int((df['위험등급'] == '관찰').sum())}명", color="yellow")
                st.badge(f"퇴소 {int((df['재적상태'] == '퇴소').sum())}명", color="gray")
    with priority_col:
        with st.container(border=True, height="stretch"):
            st.subheader("오늘 먼저 확인할 교육생")
            top_risk = risk_df.head(8)[["반", "이름", "위험등급", "주요 원인"]]
            st.dataframe(
                top_risk,
                hide_index=True,
                column_config={
                    "반": st.column_config.TextColumn("반", pinned=True, width="small"),
                    "이름": st.column_config.TextColumn("이름", pinned=True, width="small"),
                },
                height=286,
                key="top_priority_table",
            )
            st.caption("출결 기반 상대 위험도가 높은 순서입니다. 현재 값은 실제 이탈확률이 아닙니다.")
    st.space("small")
    st.subheader(f"우선 확인 대상 {len(risk_df):,}명")
    st.dataframe(
        risk_df[["반", "권역", "과정", "이름", "출석률", "출석일수", "지각·조퇴·외출", "환산결석", "환산잔여횟수", "위험등급", "주요 원인"]],
        hide_index=True,
        column_config={
            "반": st.column_config.TextColumn("반", pinned=True),
            "이름": st.column_config.TextColumn("이름", pinned=True),
            "출석률": st.column_config.ProgressColumn("출석률", format="percent", min_value=0, max_value=1.1),
            "출석일수": st.column_config.NumberColumn("출석일수", format="%d일"),
            "지각·조퇴·외출": st.column_config.NumberColumn("지각·조퇴·외출", format="%d회"),
            "환산결석": st.column_config.NumberColumn("환산 결석", format="%d회"),
            "환산잔여횟수": st.column_config.NumberColumn("다음 환산까지", format="%d/3회"),
        },
        height=520,
        key="early_warning_table",
    )

with people:
    display = filtered.sort_values(["위험점수", "출석률"], ascending=[False, True]).drop(
        columns=["반번호", "위험점수", "이탈위험도"]
    )
    event = st.dataframe(
        display,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "반": st.column_config.TextColumn("반", pinned=True),
            "이름": st.column_config.TextColumn("이름", pinned=True),
            "출석률": st.column_config.ProgressColumn("출석률", format="percent", min_value=0, max_value=1.1),
        },
        height=610,
        key="participant_table",
    )
    selected_row = event.selection.rows[0] if event.selection.rows else None
    if selected_row is not None and 0 <= selected_row < len(display):
        selected = display.iloc[selected_row]
        st.info(
            f"{selected['이름']} · {selected['반']} · {selected['위험등급']} · {selected['주요 원인']}",
            icon=":material/person:",
        )
    st.download_button(
        "현재 목록 Excel 다운로드",
        data=to_excel_bytes(display),
        file_name="교육생_통합현황.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:",
    )

with classes_view:
    summary = (
        filtered.groupby(["반번호", "반", "권역", "과정"], as_index=False)
        .agg(
            교육생=("이름", "count"),
            평균출석률=("출석률", lambda s: s.clip(upper=1).mean()),
            고위험=("위험등급", lambda s: int((s == "고위험").sum())),
            주의=("위험등급", lambda s: int((s == "주의").sum())),
            관찰=("위험등급", lambda s: int((s == "관찰").sum())),
            퇴소=("재적상태", lambda s: int((s == "퇴소").sum())),
        )
        .sort_values("반번호")
    )
    chart_data = summary[["반번호", "반", "고위험", "주의", "관찰", "퇴소"]].melt(
        id_vars=["반번호", "반"],
        value_vars=["고위험", "주의", "관찰", "퇴소"],
        var_name="위험등급",
        value_name="인원",
    )
    class_order = summary["반"].tolist()
    stacked_bars = (
        alt.Chart(chart_data)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("반:N", sort=class_order, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("sum(인원):Q", title="우선 확인 인원", axis=alt.Axis(tickMinStep=1)),
            color=alt.Color(
                "위험등급:N",
                sort=["고위험", "주의", "관찰", "퇴소"],
                scale=alt.Scale(
                    domain=["고위험", "주의", "관찰", "퇴소"],
                    range=["#B3261E", "#E37400", "#F9AB00", "#5F6368"],
                ),
                legend=alt.Legend(title=None, orient="top"),
            ),
            order=alt.Order("위험등급:N", sort="ascending"),
            tooltip=["반", "위험등급", alt.Tooltip("인원:Q", format="d")],
        )
    )
    segment_labels = (
        alt.Chart(chart_data[chart_data["인원"] > 0])
        .mark_text(color="white", fontSize=12, fontWeight="bold")
        .encode(
            x=alt.X("반:N", sort=class_order),
            y=alt.Y("sum(인원):Q", stack="center"),
            detail="위험등급:N",
            order=alt.Order("위험등급:N", sort="ascending"),
            text=alt.Text("인원:Q", format="d"),
        )
    )
    if len(summary) > 1:
        with st.container(border=True):
            st.subheader("반별 위험 신호 비교")
            st.caption("여러 반의 고위험·주의·관찰·퇴소 인원을 비교합니다.")
            st.altair_chart((stacked_bars + segment_labels).properties(height=250))
    elif len(summary) == 1:
        selected_overview = summary.iloc[0]
        with st.container(border=True):
            with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                with st.container(gap=None):
                    st.subheader(f"{selected_overview['반']} 요약")
                    st.caption(f"{selected_overview['권역']} · {selected_overview['과정']}")
                st.badge("단일 반 보기", color="blue", icon=":material/filter_alt:")
            with st.container(horizontal=True, horizontal_alignment="distribute"):
                st.metric("교육생", f"{int(selected_overview['교육생'])}명", border=True)
                st.metric("평균 출석률", f"{selected_overview['평균출석률']:.1%}", border=True)
                st.metric("고위험", f"{int(selected_overview['고위험'])}명", border=True)
                st.metric("주의", f"{int(selected_overview['주의'])}명", border=True)
                st.metric("관찰", f"{int(selected_overview['관찰'])}명", border=True)
                st.metric("퇴소", f"{int(selected_overview['퇴소'])}명", border=True)
    st.caption(":material/touch_app: 아래 표에서 반을 클릭하면 해당 반의 교육생과 위험 현황이 열립니다.")
    class_event = st.dataframe(
        summary.drop(columns="반번호"),
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "반": st.column_config.TextColumn("반", pinned=True),
            "평균출석률": st.column_config.ProgressColumn("평균 출석률", format="percent", min_value=0, max_value=1),
            "고위험": st.column_config.NumberColumn("고위험", format="%d명"),
            "주의": st.column_config.NumberColumn("주의", format="%d명"),
            "관찰": st.column_config.NumberColumn("관찰", format="%d명"),
        },
        height=330,
        key="class_summary",
    )
    displayed_summary = summary.drop(columns="반번호")
    selected_class_row = class_event.selection.rows[0] if class_event.selection.rows else None
    if selected_class_row is not None and 0 <= selected_class_row < len(displayed_summary):
        selected_summary = displayed_summary.iloc[selected_class_row]
        selected_class = selected_summary["반"]
        class_df = df[df["반"] == selected_class].sort_values(
            ["위험점수", "출석률"], ascending=[False, True]
        )

        st.space("small")
        with st.container(border=True):
            with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                with st.container(gap=None):
                    st.subheader(f"{selected_class} 상세 현황")
                    st.caption(f"{selected_summary['권역']} · {selected_summary['과정']}")
                st.badge(
                    f"고위험 {int((class_df['위험등급'] == '고위험').sum())}명",
                    color="red" if (class_df["위험등급"] == "고위험").any() else "green",
                    icon=":material/warning:",
                )

            with st.container(horizontal=True, horizontal_alignment="distribute"):
                st.metric("교육생", f"{len(class_df)}명", border=True)
                st.metric("평균 출석률", f"{class_df['출석률'].clip(upper=1).mean():.1%}", border=True)
                st.metric("고위험", f"{(class_df['위험등급'] == '고위험').sum()}명", border=True)
                st.metric("주의·관찰", f"{class_df['위험등급'].isin(['주의', '관찰']).sum()}명", border=True)

            detail_chart_col, detail_table_col = st.columns([0.8, 2.2], gap="large")
            with detail_chart_col:
                class_risk = (
                    class_df["위험등급"]
                    .value_counts()
                    .reindex(["고위험", "주의", "관찰", "정상", "퇴소"], fill_value=0)
                    .rename_axis("위험등급")
                    .reset_index(name="인원")
                )
                class_chart = (
                    alt.Chart(class_risk)
                    .mark_arc(innerRadius=48, outerRadius=82)
                    .encode(
                        theta=alt.Theta("인원:Q"),
                        color=alt.Color(
                            "위험등급:N",
                            scale=alt.Scale(
                                domain=["고위험", "주의", "관찰", "정상", "퇴소"],
                                range=["#B3261E", "#E37400", "#F9AB00", "#188038", "#5F6368"],
                            ),
                            legend=alt.Legend(title=None, orient="bottom"),
                        ),
                        tooltip=["위험등급", "인원"],
                    )
                    .properties(height=280, title="위험등급 구성")
                )
                st.altair_chart(class_chart)
            with detail_table_col:
                st.dataframe(
                    class_df[["이름", "출석률", "출석일수", "지각·조퇴·외출", "환산결석", "환산잔여횟수", "위험등급", "주요 원인"]],
                    hide_index=True,
                    column_config={
                        "이름": st.column_config.TextColumn("이름", pinned=True),
                        "출석률": st.column_config.ProgressColumn("출석률", format="percent", min_value=0, max_value=1.1),
                        "출석일수": st.column_config.NumberColumn("출석일수", format="%d일"),
                        "지각·조퇴·외출": st.column_config.NumberColumn("지각·조퇴·외출", format="%d회"),
                        "환산결석": st.column_config.NumberColumn("환산 결석", format="%d회"),
                        "환산잔여횟수": st.column_config.NumberColumn("다음 환산까지", format="%d/3회"),
                    },
                    height=360,
                    key=f"class_detail_{selected_class}",
                )
    else:
        st.info("확인할 반의 행을 클릭해 주세요.", icon=":material/touch_app:")

with manager_view:
    active_students = df[df["재적상태"] != "퇴소"].copy()
    dropouts = df[df["재적상태"] == "퇴소"].sort_values(["반번호", "이름"]).copy()
    priority_count = max(1, ceil(len(active_students) * 0.02))
    priority = (
        active_students.sort_values(["위험점수", "출석률", "지각·조퇴·외출"], ascending=[False, True, False])
        .head(priority_count)
        .copy()
    )
    priority.insert(0, "순위", range(1, len(priority) + 1))

    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        with st.container(gap=None):
            st.subheader("우선관리 상위 2%")
            st.caption("전체 교육생 중 출석률과 지각·조퇴·외출 신호가 상대적으로 높은 순서입니다.")
        st.badge(f"{len(priority)}명", color="red", icon=":material/priority_high:")

    with st.container(horizontal=True, horizontal_alignment="distribute"):
        st.metric("우선관리 인원", f"{len(priority)}명", border=True)
        st.metric("평균 출석률", f"{priority['출석률'].clip(upper=1).mean():.1%}", border=True)
        st.metric("80% 미만", f"{(priority['출석률'] < 0.8).sum()}명", border=True)
        st.metric("환산 결석", f"{priority['환산결석'].sum():.0f}회", border=True)
        st.metric("퇴소자", f"{len(dropouts)}명", border=True)

    st.dataframe(
        priority[
            [
                "순위",
                "반",
                "권역",
                "과정",
                "이름",
                "출석률",
                "출석일수",
                "지각·조퇴·외출",
                "환산결석",
                "주요 원인",
            ]
        ],
        hide_index=True,
        column_config={
            "순위": st.column_config.NumberColumn("순위", format="%d", width="small", pinned=True),
            "반": st.column_config.TextColumn("반", width="small", pinned=True),
            "이름": st.column_config.TextColumn("이름", width="small", pinned=True),
            "출석률": st.column_config.ProgressColumn(
                "출석률", format="percent", min_value=0, max_value=1
            ),
            "출석일수": st.column_config.NumberColumn("출석일수", format="%d일"),
            "지각·조퇴·외출": st.column_config.NumberColumn("지각·조퇴·외출", format="%d회"),
            "환산결석": st.column_config.NumberColumn("환산 결석", format="%d회"),
        },
        height=min(650, 38 + len(priority) * 35),
        key="priority_top_ten_table",
    )

    recent_dates = sorted(daily_df["날짜"].dropna().unique())[-10:]
    recent_priority = daily_df.merge(priority[["반", "이름"]], on=["반", "이름"], how="inner")
    recent_priority = recent_priority[recent_priority["날짜"].isin(recent_dates)]
    if not recent_priority.empty:
        status_matrix = (
            recent_priority.pivot_table(
                index=["반", "이름"], columns="날짜", values="상태", aggfunc="last", fill_value="-"
            )
            .reset_index()
        )
        status_matrix.columns = [
            value.strftime("%m/%d") if isinstance(value, (pd.Timestamp, datetime)) else str(value)
            for value in status_matrix.columns
        ]
        with st.container(border=True):
            st.markdown("**최근 10일 출결 흐름**")
            st.caption("우선관리 대상의 일별 상태를 한 화면에서 비교합니다.")
            st.dataframe(
                status_matrix,
                hide_index=True,
                column_config={
                    "반": st.column_config.TextColumn("반", pinned=True),
                    "이름": st.column_config.TextColumn("이름", pinned=True),
                },
                height=min(520, 38 + len(status_matrix) * 35),
                key="priority_recent_status",
            )

    st.divider()
    current_email = str(st.session_state.get("current_user_email", "")).strip().lower()
    try:
        admin_emails = EDITOR_EMAILS | {
            str(value).strip().lower()
            for value in st.secrets.get("ADMIN_EMAILS", ["hint.soyun@gmail.com"])
            if str(value).strip()
        }
    except StreamlitSecretNotFoundError:
        admin_emails = {"local-admin"} | EDITOR_EMAILS
    is_admin = current_email in admin_emails or current_email == "local-admin"
    report_credentials = _report_service_account_credentials()
    report_history = pd.DataFrame(columns=REPORT_HEADERS)

    st.subheader("관리자 데일리 리포트")
    if report_credentials is None:
        st.info(
            "리포트 저장 연결을 준비 중입니다. Streamlit 비밀 설정이 완료되면 작성 기능이 열립니다.",
            icon=":material/settings:",
        )
    elif is_admin:
        latest_date = daily_df["날짜"].max().date() if not daily_df.empty else date.today()
        latest_rows = daily_df[daily_df["날짜"].dt.date == latest_date]
        absence_count = int((latest_rows["상태"] == "결석").sum())
        event_count = int(latest_rows["상태"].isin(["지각", "조퇴", "외출"]).sum())
        with st.form("manager_daily_report", border=True):
            with st.container(horizontal=True):
                report_date = st.date_input("기준일", value=latest_date, key="report_date")
                author = st.text_input("작성자", value=current_email, disabled=True)
            st.caption(f"기준일 자동 요약: 결석 {absence_count}명 · 지각·조퇴·외출 {event_count}명")
            summary_text = st.text_area(
                "전체 요약",
                placeholder="오늘 출결 상황과 평소 대비 변화 내용을 적어주세요.",
                key="report_summary",
            )
            cause_text = st.text_area(
                "결석·이상 증가 원인",
                placeholder="특정 반 행사, 신규 입과, 시스템 입력 지연 등 원인을 적어주세요.",
                key="report_cause",
            )
            special_text = st.text_area("특이사항", key="report_special")
            followup_text = st.text_area("추후 확인사항", key="report_followup")
            submitted = st.form_submit_button(
                "데일리 리포트 저장", type="primary", icon=":material/save:"
            )
        if submitted:
            if not summary_text.strip():
                st.error("전체 요약을 입력해 주세요.")
            else:
                save_daily_report(
                    REPORT_SHEET_ID,
                    report_credentials,
                    [
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        report_date.isoformat(),
                        current_email,
                        summary_text.strip(),
                        cause_text.strip(),
                        special_text.strip(),
                        "",
                        followup_text.strip(),
                    ],
                )
                st.success("데일리 리포트를 저장했습니다.", icon=":material/check_circle:")
    else:
        st.caption("관리자 계정만 리포트를 작성할 수 있습니다. 저장된 리포트는 아래에서 조회할 수 있습니다.")

    if report_credentials is not None:
        try:
            report_history = load_daily_reports(REPORT_SHEET_ID, report_credentials)
            with st.container(border=True):
                st.markdown("**최근 작성 리포트**")
                if report_history.empty:
                    st.caption("아직 저장된 리포트가 없습니다.")
                else:
                    st.dataframe(
                        report_history.sort_values("작성일시", ascending=False),
                        hide_index=True,
                        column_config={
                            "작성일시": st.column_config.TextColumn("작성일시", pinned=True),
                            "기준일": st.column_config.DateColumn("기준일"),
                        },
                        height=min(420, 38 + len(report_history) * 35),
                        key="daily_report_history",
                    )
        except Exception as error:
            st.warning(f"저장된 리포트를 불러오지 못했습니다: {error}", icon=":material/cloud_off:")

    with st.container(border=True):
        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            with st.container(gap=None):
                st.markdown("**PDF 리포트 출력**")
                st.caption("핵심 지표, 반별 그래프, 출석률 그래프와 우선관리 명단을 한 파일로 저장합니다.")
            st.download_button(
                "PDF 다운로드",
                data=build_priority_pdf(
                    priority.drop(columns="순위"),
                    dropouts,
                    report_history,
                    daily_df["날짜"].max().strftime("%Y-%m-%d"),
                ),
                file_name=f"K뉴딜_우선관리_리포트_{daily_df['날짜'].max().strftime('%Y%m%d')}.pdf",
                mime="application/pdf",
                icon=":material/picture_as_pdf:",
                type="primary",
            )

with period_view:
    st.subheader("일자·주간·월간 출결")
    st.caption("원본 시트의 DAY별 기록을 기준으로 집계합니다. 해당없음과 아직 기록되지 않은 미래 일정은 제외됩니다.")

    period_data = daily_df.copy()
    if query:
        period_data = period_data[period_data["이름"].str.contains(query.strip(), case=False, na=False)]
    if regions:
        period_data = period_data[period_data["권역"].isin(regions)]
    if classes:
        period_data = period_data[period_data["반"].isin(classes)]

    available_start = period_data["날짜"].min().date()
    available_end = period_data["날짜"].max().date()
    with st.container(horizontal=True, vertical_alignment="bottom"):
        period_unit = st.segmented_control(
            "집계 단위",
            ["일자별", "주간", "월간"],
            default="일자별",
            key="attendance_period_unit",
        )
        selected_dates = st.date_input(
            "조회 기간",
            value=(available_start, available_end),
            min_value=available_start,
            max_value=available_end,
            key="attendance_date_range",
        )

    if isinstance(selected_dates, (tuple, list)) and len(selected_dates) == 2:
        start_date, end_date = selected_dates
    else:
        start_date = end_date = selected_dates
    period_data = period_data[
        period_data["날짜"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    ].copy()

    if period_data.empty:
        st.info("선택한 조건에 출결 기록이 없습니다.", icon=":material/event_busy:")
    else:
        if period_unit == "주간":
            week_start = period_data["날짜"] - pd.to_timedelta(period_data["날짜"].dt.weekday, unit="D")
            period_data["기간"] = week_start.dt.strftime("%m/%d") + "~" + (week_start + pd.Timedelta(days=6)).dt.strftime("%m/%d")
        elif period_unit == "월간":
            period_data["기간"] = period_data["날짜"].dt.strftime("%Y년 %m월")
        else:
            period_data["기간"] = period_data["날짜"].dt.strftime("%m/%d")

        summary_period = summarize_attendance(period_data, "기간")
        event_total = int(period_data["상태"].isin(["지각", "조퇴", "외출"]).sum())
        absence_total = int((period_data["상태"] == "결석").sum() + event_total // 3)
        attendance_rate = max(0.0, 1 - absence_total / max(len(period_data), 1))

        with st.container(horizontal=True, horizontal_alignment="distribute"):
            st.metric("조회 기록", f"{len(period_data):,}건", border=True)
            st.metric("환산 출석률", f"{attendance_rate:.1%}", border=True)
            st.metric("결석", f"{int((period_data['상태'] == '결석').sum()):,}건", border=True)
            st.metric("지각·조퇴·외출", f"{event_total:,}건", border=True)
            st.metric("총 환산결석", f"{absence_total:,}건", border=True)

        trend = summary_period.melt(
            id_vars=["기간"],
            value_vars=["출석", "인정출석", "결석", "지각·조퇴·외출"],
            var_name="상태",
            value_name="건수",
        )
        trend_chart = (
            alt.Chart(trend)
            .mark_line(point=True, strokeWidth=3)
            .encode(
                x=alt.X("기간:N", title=None, sort=None, axis=alt.Axis(labelAngle=-35)),
                y=alt.Y("건수:Q", title="건수", axis=alt.Axis(tickMinStep=1)),
                color=alt.Color(
                    "상태:N",
                    scale=alt.Scale(
                        domain=["출석", "인정출석", "결석", "지각·조퇴·외출"],
                        range=["#1967D2", "#188038", "#B3261E", "#E37400"],
                    ),
                    legend=alt.Legend(title=None, orient="top"),
                ),
                tooltip=["기간", "상태", alt.Tooltip("건수:Q", format="d")],
            )
            .properties(height=310)
        )
        with st.container(border=True):
            st.subheader(f"{period_unit} 출결 추이")
            st.altair_chart(trend_chart)

        st.dataframe(
            summary_period[["기간", "출석", "인정출석", "결석", "지각·조퇴·외출", "환산결석", "환산출석률"]],
            hide_index=True,
            column_config={
                "기간": st.column_config.TextColumn("기간", pinned=True),
                "출석": st.column_config.NumberColumn("출석", format="%d건"),
                "인정출석": st.column_config.NumberColumn("인정출석", format="%d건"),
                "결석": st.column_config.NumberColumn("결석", format="%d건"),
                "지각·조퇴·외출": st.column_config.NumberColumn("지각·조퇴·외출", format="%d건"),
                "환산결석": st.column_config.NumberColumn("환산결석", format="%d건"),
                "환산출석률": st.column_config.ProgressColumn("환산 출석률", format="percent", min_value=0, max_value=1),
            },
            key="period_summary_table",
        )

        with st.expander("학생별 상세 기록"):
            detail = period_data[["날짜", "반", "권역", "과정", "이름", "상태"]].sort_values(
                ["날짜", "반", "이름"], ascending=[False, True, True]
            )
            st.dataframe(
                detail,
                hide_index=True,
                column_config={
                    "날짜": st.column_config.DateColumn("날짜", format="YYYY-MM-DD"),
                    "반": st.column_config.TextColumn("반", pinned=True),
                    "이름": st.column_config.TextColumn("이름", pinned=True),
                },
                height=420,
                key="daily_attendance_detail",
            )
