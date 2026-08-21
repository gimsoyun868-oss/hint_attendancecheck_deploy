from __future__ import annotations

from io import BytesIO

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


FONT_REGULAR = "HYSMyeongJo-Medium"
FONT_BOLD = "HYSMyeongJo-Medium"
NAVY = colors.HexColor("#17324D")
BLUE = colors.HexColor("#246BCE")
RED = colors.HexColor("#C62828")
ORANGE = colors.HexColor("#EF6C00")
GREEN = colors.HexColor("#188038")
LIGHT_BLUE = colors.HexColor("#EAF2FF")
LIGHT_GRAY = colors.HexColor("#F4F6F8")
GRID = colors.HexColor("#D8DEE8")


def _register_fonts() -> None:
    for name in {FONT_REGULAR, FONT_BOLD}:
        try:
            pdfmetrics.getFont(name)
        except KeyError:
            pdfmetrics.registerFont(UnicodeCIDFont(name))


def _paragraph(text, style):
    safe = str(text or "-").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(safe, style)


def _page_number(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(GRID)
    canvas.line(16 * mm, 12 * mm, landscape(A4)[0] - 16 * mm, 12 * mm)
    canvas.setFont(FONT_REGULAR, 8)
    canvas.setFillColor(colors.HexColor("#667085"))
    canvas.drawString(16 * mm, 7 * mm, "K-뉴딜 아카데미 출결 관리 리포트")
    canvas.drawRightString(landscape(A4)[0] - 16 * mm, 7 * mm, f"{doc.page} page")
    canvas.restoreState()


def _kpi_table(priority, dropouts, styles):
    rate = priority["출석률"].clip(upper=1).mean() if len(priority) else 0
    values = [
        ("우선관리", f"{len(priority)}명"),
        ("평균 출석률", f"{rate:.1%}"),
        ("80% 미만", f"{int((priority['출석률'] < 0.8).sum())}명"),
        ("환산 결석", f"{int(priority['환산결석'].sum())}회"),
        ("퇴소자", f"{len(dropouts)}명"),
    ]
    cells = []
    for label, value in values:
        cells.append(
            [
                _paragraph(label, styles["kpi_label"]),
                _paragraph(value, styles["kpi_value"]),
            ]
        )
    table = Table([sum(cells, [])], colWidths=[24 * mm, 29 * mm] * 5, rowHeights=23 * mm)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GRAY),
                ("BOX", (0, 0), (-1, -1), 0.7, GRID),
                ("INNERGRID", (0, 0), (-1, -1), 0.7, colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _class_chart(priority, dropouts) -> Drawing:
    def class_number(label):
        digits = "".join(character for character in str(label) if character.isdigit())
        return int(digits or 999)

    labels = sorted(set(priority["반"].tolist()) | set(dropouts["반"].tolist()), key=class_number)
    priority_counts = priority["반"].value_counts().to_dict()
    dropout_counts = dropouts["반"].value_counts().to_dict()
    values = [int(priority_counts.get(label, 0)) for label in labels]
    dropout_values = [int(dropout_counts.get(label, 0)) for label in labels]
    drawing = Drawing(365, 205)
    drawing.add(String(8, 190, "반별 우선관리·퇴소 인원", fontName=FONT_BOLD, fontSize=11, fillColor=NAVY))
    drawing.add(Rect(205, 188, 7, 7, fillColor=BLUE, strokeColor=BLUE))
    drawing.add(String(216, 189, "우선관리", fontName=FONT_REGULAR, fontSize=7, fillColor=NAVY))
    drawing.add(Rect(274, 188, 7, 7, fillColor=colors.HexColor("#5F6368"), strokeColor=colors.HexColor("#5F6368")))
    drawing.add(String(285, 189, "퇴소", fontName=FONT_REGULAR, fontSize=7, fillColor=NAVY))
    chart = VerticalBarChart()
    chart.x, chart.y, chart.width, chart.height = 36, 35, 315, 135
    chart.data = [values, dropout_values]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontName = FONT_REGULAR
    chart.categoryAxis.labels.fontSize = 6.5
    chart.categoryAxis.labels.angle = 30
    chart.categoryAxis.labels.dy = -8
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max(max(values + dropout_values, default=1) + 1, 3)
    chart.valueAxis.valueStep = 1
    chart.valueAxis.labels.fontName = FONT_REGULAR
    chart.valueAxis.labels.fontSize = 7
    chart.bars[0].fillColor = BLUE
    chart.bars[0].strokeColor = BLUE
    chart.bars[1].fillColor = colors.HexColor("#5F6368")
    chart.bars[1].strokeColor = colors.HexColor("#5F6368")
    drawing.add(chart)
    return drawing


def _attendance_chart(priority) -> Drawing:
    chart_frame = priority.sort_values("출석률", ascending=True).head(12)
    labels = (chart_frame["반"].astype(str) + " " + chart_frame["이름"].astype(str)).tolist()
    values = (chart_frame["출석률"].clip(0, 1) * 100).round(1).tolist()
    drawing = Drawing(365, 205)
    drawing.add(String(8, 190, "우선관리 대상 출석률", fontName=FONT_BOLD, fontSize=11, fillColor=NAVY))
    chart = HorizontalBarChart()
    chart.x, chart.y, chart.width, chart.height = 92, 26, 255, 145
    chart.data = [values]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontName = FONT_REGULAR
    chart.categoryAxis.labels.fontSize = 6.5
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = 100
    chart.valueAxis.valueStep = 20
    chart.valueAxis.labels.fontName = FONT_REGULAR
    chart.valueAxis.labels.fontSize = 7
    chart.bars[0].fillColor = ORANGE
    chart.bars[0].strokeColor = ORANGE
    drawing.add(chart)
    return drawing


def _priority_table(priority, styles):
    headers = ["순위", "반", "이름", "출석률", "출석일수", "지각·조퇴·외출", "환산결석", "주요 원인"]
    rows = [[_paragraph(value, styles["table_header"]) for value in headers]]
    for index, (_, row) in enumerate(priority.iterrows(), start=1):
        rows.append(
            [
                str(index),
                row["반"],
                row["이름"],
                f"{min(float(row['출석률']), 1):.1%}",
                f"{int(row['출석일수'])}일",
                f"{int(row['지각·조퇴·외출'])}회",
                f"{int(row['환산결석'])}회",
                _paragraph(row["주요 원인"], styles["table_cell"]),
            ]
        )
    table = Table(
        rows,
        repeatRows=1,
        colWidths=[13 * mm, 17 * mm, 20 * mm, 22 * mm, 22 * mm, 31 * mm, 24 * mm, 105 * mm],
    )
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 1), (-1, -1), FONT_REGULAR),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (6, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for row_index in range(1, len(rows)):
        if row_index % 2 == 0:
            commands.append(("BACKGROUND", (0, row_index), (-1, row_index), LIGHT_GRAY))
    table.setStyle(TableStyle(commands))
    return table


def _dropout_table(dropouts, styles):
    headers = ["반", "권역", "과정", "이름", "퇴소 시점 출석률", "출석일수", "재적상태"]
    rows = [[_paragraph(value, styles["table_header"]) for value in headers]]
    for _, row in dropouts.iterrows():
        rows.append(
            [
                row["반"],
                row["권역"],
                row["과정"],
                row["이름"],
                f"{min(float(row['출석률']), 1):.1%}",
                f"{int(row['출석일수'])}일",
                "퇴소",
            ]
        )
    table = Table(rows, repeatRows=1, colWidths=[20 * mm, 28 * mm, 76 * mm, 28 * mm, 38 * mm, 30 * mm, 28 * mm])
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3C4043")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 1), (-1, -1), FONT_REGULAR),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for row_index in range(1, len(rows)):
        if row_index % 2 == 0:
            commands.append(("BACKGROUND", (0, row_index), (-1, row_index), LIGHT_GRAY))
    table.setStyle(TableStyle(commands))
    return table


def build_priority_pdf(priority, dropouts, report_history, as_of: str) -> bytes:
    _register_fonts()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleKo", fontName=FONT_BOLD, fontSize=22, leading=27, textColor=NAVY))
    styles.add(ParagraphStyle(name="SubtitleKo", fontName=FONT_REGULAR, fontSize=9, leading=14, textColor=colors.HexColor("#667085")))
    styles.add(ParagraphStyle(name="SectionKo", fontName=FONT_BOLD, fontSize=14, leading=18, textColor=NAVY, spaceAfter=7))
    styles.add(ParagraphStyle(name="kpi_label", fontName=FONT_REGULAR, fontSize=8, leading=10, textColor=colors.HexColor("#667085"), alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="kpi_value", fontName=FONT_BOLD, fontSize=15, leading=18, textColor=NAVY, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="table_header", fontName=FONT_BOLD, fontSize=8, leading=10, textColor=colors.white, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="table_cell", fontName=FONT_REGULAR, fontSize=7.5, leading=10, alignment=TA_LEFT))
    styles.add(ParagraphStyle(name="BodyKo", fontName=FONT_REGULAR, fontSize=9, leading=14, textColor=colors.HexColor("#344054")))

    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=17 * mm,
        title="K-뉴딜 아카데미 우선관리 리포트",
        author="HINT",
    )
    story = [
        Paragraph("K-뉴딜 아카데미 우선관리 리포트", styles["TitleKo"]),
        Paragraph(f"기준일 {as_of} | 출결 기반 우선관리 상위 2%", styles["SubtitleKo"]),
        Spacer(1, 6 * mm),
        _kpi_table(priority, dropouts, styles),
        Spacer(1, 6 * mm),
        Table([[_class_chart(priority, dropouts), _attendance_chart(priority)]], colWidths=[132 * mm, 132 * mm]),
        Spacer(1, 3 * mm),
        Paragraph("※ 이 자료는 규칙 기반 관리 우선순위이며 실제 중도탈락 확률이 아닙니다.", styles["SubtitleKo"]),
        PageBreak(),
        Paragraph("우선관리 대상 상세 명단", styles["SectionKo"]),
        _priority_table(priority, styles),
    ]

    if dropouts is not None and not dropouts.empty:
        story.extend(
            [
                PageBreak(),
                Paragraph("퇴소자 현황", styles["SectionKo"]),
                Paragraph("원본 구글 시트에서 이름 셀이 검은색으로 표시된 교육생입니다.", styles["SubtitleKo"]),
                Spacer(1, 3 * mm),
                _dropout_table(dropouts, styles),
            ]
        )

    if report_history is not None and not report_history.empty:
        latest = report_history.sort_values("작성일시", ascending=False).iloc[0]
        report_rows = [
            ("기준일", latest.get("기준일", "-")),
            ("작성자", latest.get("작성자", "-")),
            ("전체 요약", latest.get("전체 요약", "-")),
            ("결석·이상 증가 원인", latest.get("결석·이상 증가 원인", "-")),
            ("특이사항", latest.get("특이사항", "-")),
            ("추후 확인사항", latest.get("추후 확인사항", "-")),
        ]
        report_table = Table(
            [[_paragraph(label, styles["table_header"]), _paragraph(value, styles["BodyKo"])] for label, value in report_rows],
            colWidths=[44 * mm, 210 * mm],
        )
        report_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), NAVY),
                    ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
                    ("BACKGROUND", (1, 0), (1, -1), LIGHT_GRAY),
                    ("GRID", (0, 0), (-1, -1), 0.5, GRID),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.extend([PageBreak(), Paragraph("최근 관리자 데일리 리포트", styles["SectionKo"]), KeepTogether(report_table)])

    doc.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    return output.getvalue()


def _operations_chart(class_summary) -> Drawing:
    frame = class_summary.copy()
    labels = frame["반"].astype(str).tolist()
    values = (frame["출석률"].fillna(0).clip(0, 1) * 100).round(1).tolist()
    drawing = Drawing(735, 210)
    drawing.add(String(8, 194, "17개 반 출석률", fontName=FONT_BOLD, fontSize=12, fillColor=NAVY))
    chart = VerticalBarChart()
    chart.x, chart.y, chart.width, chart.height = 45, 35, 665, 140
    chart.data = [values]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontName = FONT_REGULAR
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.angle = 25
    chart.categoryAxis.labels.dy = -7
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = 100
    chart.valueAxis.valueStep = 20
    chart.valueAxis.labels.fontName = FONT_REGULAR
    chart.valueAxis.labels.fontSize = 7
    chart.bars[0].fillColor = BLUE
    chart.bars[0].strokeColor = BLUE
    drawing.add(chart)
    return drawing


def build_operations_pdf(class_summary, operation_logs, title: str, period_label: str, admin_note: str = "") -> bytes:
    """Build an administrator-only daily or weekly operations report."""
    _register_fonts()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="OpsTitle", fontName=FONT_BOLD, fontSize=21, leading=26, textColor=NAVY))
    styles.add(ParagraphStyle(name="OpsSubtitle", fontName=FONT_REGULAR, fontSize=9, leading=14, textColor=colors.HexColor("#667085")))
    styles.add(ParagraphStyle(name="OpsSection", fontName=FONT_BOLD, fontSize=14, leading=18, textColor=NAVY, spaceAfter=7))
    styles.add(ParagraphStyle(name="OpsHeader", fontName=FONT_BOLD, fontSize=7.5, leading=9, textColor=colors.white, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="OpsCell", fontName=FONT_REGULAR, fontSize=7, leading=9, textColor=colors.HexColor("#344054")))
    styles.add(ParagraphStyle(name="OpsBody", fontName=FONT_REGULAR, fontSize=9, leading=14, textColor=colors.HexColor("#344054")))

    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=17 * mm,
        title=title,
        author="HINT",
    )

    submitted = int(class_summary["제출여부"].sum()) if "제출여부" in class_summary else 0
    average_rate = class_summary["출석률"].mean() if len(class_summary) else 0
    absences = int(class_summary["결석"].sum()) if "결석" in class_summary else 0
    attention = int((class_summary["상태"] == "확인 필요").sum()) if "상태" in class_summary else 0
    kpis = Table(
        [[
            _paragraph("제출 완료", styles["OpsSubtitle"]), _paragraph(f"{submitted}개 반", styles["OpsSection"]),
            _paragraph("평균 출석률", styles["OpsSubtitle"]), _paragraph(f"{average_rate:.1%}", styles["OpsSection"]),
            _paragraph("결석", styles["OpsSubtitle"]), _paragraph(f"{absences}명", styles["OpsSection"]),
            _paragraph("확인 필요", styles["OpsSubtitle"]), _paragraph(f"{attention}개 반", styles["OpsSection"]),
        ]],
        colWidths=[28 * mm, 34 * mm] * 4,
        rowHeights=20 * mm,
    )
    kpis.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GRAY),
        ("BOX", (0, 0), (-1, -1), 0.7, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))

    headers = ["반", "권역", "과정", "출석률", "결석", "지각·조퇴·외출", "운영점수", "제출", "상태", "특이사항"]
    rows = [[_paragraph(value, styles["OpsHeader"]) for value in headers]]
    for _, row in class_summary.iterrows():
        rows.append([
            row.get("반", "-"), row.get("권역", "-"), _paragraph(row.get("과정", "-"), styles["OpsCell"]),
            f"{float(row.get('출석률', 0)):.1%}", f"{int(row.get('결석', 0))}명",
            f"{int(row.get('지각조퇴외출', 0))}명",
            f"{float(row.get('운영점수', 0)):.1f}" if row.get("제출여부", False) else "-",
            "완료" if row.get("제출여부", False) else "미제출", row.get("상태", "-"),
            _paragraph(row.get("특이사항", "-"), styles["OpsCell"]),
        ])
    summary_table = Table(
        rows, repeatRows=1,
        colWidths=[14 * mm, 20 * mm, 44 * mm, 19 * mm, 15 * mm, 27 * mm, 21 * mm, 18 * mm, 23 * mm, 59 * mm],
    )
    summary_commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 1), (-1, -1), FONT_REGULAR), ("FONTSIZE", (0, 1), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (8, -1), "CENTER"), ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row_index in range(1, len(rows)):
        if row_index % 2 == 0:
            summary_commands.append(("BACKGROUND", (0, row_index), (-1, row_index), LIGHT_GRAY))
        if str(class_summary.iloc[row_index - 1].get("상태", "")) == "확인 필요":
            summary_commands.append(("TEXTCOLOR", (8, row_index), (8, row_index), RED))
    summary_table.setStyle(TableStyle(summary_commands))

    story = [
        Paragraph(title, styles["OpsTitle"]),
        Paragraph(f"기간 {period_label} | 최종관리자용", styles["OpsSubtitle"]),
        Spacer(1, 5 * mm), kpis, Spacer(1, 4 * mm), _operations_chart(class_summary),
        PageBreak(), Paragraph("17개 반 통합 현황", styles["OpsSection"]), summary_table,
    ]
    if admin_note.strip():
        note_table = Table(
            [[_paragraph("관리자 총평", styles["OpsHeader"]), _paragraph(admin_note, styles["OpsBody"])]],
            colWidths=[34 * mm, 220 * mm],
        )
        note_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), NAVY), ("TEXTCOLOR", (0, 0), (0, 0), colors.white),
            ("BACKGROUND", (1, 0), (1, 0), LIGHT_BLUE), ("GRID", (0, 0), (-1, -1), 0.5, GRID),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.extend([Spacer(1, 5 * mm), KeepTogether(note_table)])

    if operation_logs is not None and not operation_logs.empty:
        issue_columns = ["기준일", "반", "작성자", "항목별특이사항", "출결특이사항", "면담결과", "기타특이사항"]
        issue_rows = [[_paragraph(value, styles["OpsHeader"]) for value in issue_columns]]
        for _, row in operation_logs.iterrows():
            issue_rows.append([_paragraph(row.get(column, "-"), styles["OpsCell"]) for column in issue_columns])
        issues_table = Table(
            issue_rows, repeatRows=1,
            colWidths=[24 * mm, 16 * mm, 36 * mm, 45 * mm, 45 * mm, 45 * mm, 45 * mm],
        )
        issues_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.extend([PageBreak(), Paragraph("반별 특이사항 및 면담", styles["OpsSection"]), issues_table])

    doc.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    return output.getvalue()

