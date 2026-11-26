# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generate the accelerator's architecture diagram, in the light (docs) or dark (decks) palette.

    python3 -I docs/start/architecture/build.py           # both themes
    python3 -I docs/start/architecture/build.py light     # one: light, dark or slide

Writes architecture.<theme>.drawio.png beside this file: a PNG with the diagram embedded, so
draw.io opens it for editing. Edit this file rather than the PNG, so a rebuild keeps the change.
Needs the draw.io desktop app's CLI (`drawio`) on PATH.

The main path starts at the poller, which reads SAP on a schedule and makes the cases; SAP
pushing events along the top (the SAP layer's bus, then the use case's ingest queue) is
optional, so it is drawn dashed. The agent runs on the platform, and its calls go back out along
the bottom (the Gateway's checks, then the SAP layer's MCP server). Each part has its own color, and a
box inside a shared service takes the color of the part that owns it. All coordinates are
absolute px.
"""

import base64
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
from xml.sax.saxutils import quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = "fontFamily=Amazon Ember,Arial;"
DISPLAY = "Amazon Ember Display,Amazon Ember,Arial"  # headings and names
F, G = 19, 22  # body and group-title px
# The layout is drawn on a 2420 px grid and stretched sideways by SX, for room between boxes.
# Boxes and text cells stretch; icons, badges and glyphs keep their size and move with their centre.
SX = 1.2
# Extra px opened at these grid x positions: in pairs either side of each badge or boundary
# between parts, so the arrows that cross from one part to the next have room.
GAPS = (
    (315, 30),
    (345, 30),
    (810, 30),
    (830, 30),
    (1610, 20),
    (1630, 20),
    (2060, 20),
    (2100, 20),
)


# Shorter names where a word would not fit its box at slide size.
SLIDE_NAMES = {
    "Notification": "Email or tickets",
    "Cedar policy engine": "Cedar policies",
    "Knowledge": "Search",
    "SAP access claim check": "SAP access check",
}


# Slide only: extra px opened below these grid y positions, for titles at slide size.
YGAPS = ((95, 14), (600, 20), (650, 16), (1000, 50))


def sx(x):
    return x * SX + sum(extra for at, extra in GAPS if x > at)


FIXED = (
    "resourceIcon",
    "ellipse;",
    "shape=image",
    "agent2",
    "authenticated_user",
    "shape=cube",
    "shape=document",
)

THEMES = {
    "light": {
        "bg": "#FFFFFF",
        "box_fill": "#F4F6F8",
        "box_ln": "#C6CDD5",
        "grp_ln": "#8A96A3",
        "hl": "#6E3FF3",
        "arrow": "#5F6B7A",
        "main": "#6E3FF3",
        "txt": "#161D26",
        "cap": "#4B5563",
        "step": "#6E3FF3",
        "step2": "#0972D3",
        "plat": "#067F68",
        "plat_tint": "#E8F6F2",
        "sap": "#0972D3",
        "sap_tint": "#EAF3FC",
        "uc": "#B0127A",
        "uc_tint": "#FBEAF3",
        "user": "#0972D3",
    },
    "dark": {
        "bg": "#000000",
        "box_fill": "#10263B",
        "box_ln": "#344456",
        "grp_ln": "#9BA8B7",
        "hl": "#B9A2FF",
        "arrow": "#9BA8B7",
        "main": "#B9A2FF",
        "txt": "#FFFFFF",
        "cap": "#D5DBE2",
        "step": "#8A5CFF",
        "step2": "#41B3FF",
        "plat": "#2BC4A4",
        "plat_tint": "#082A24",
        "sap": "#41B3FF",
        "sap_tint": "#0A2238",
        "uc": "#FF6FB5",
        "uc_tint": "#2E0C20",
        "user": "#41B3FF",
    },
}


def build(theme):
    """`slide` is the dark palette with names only, sized so text lands at 9 pt or more on a slide."""
    slide = theme == "slide"
    dark = theme in ("dark", "slide")
    T = THEMES["dark" if dark else theme]
    F, G = (31, 32) if slide else (19, 22)
    ygaps = YGAPS if slide else ()

    def sy(y):
        return y + sum(extra for at, extra in ygaps if y > at)

    cells = []
    origin = {"1": (0.0, 0.0)}

    def vtx(cid, parent, box, value="", style="", raw=False):
        x, y, w, h = box
        if raw:
            pass
        elif any(k in style for k in FIXED):
            x, y = sx(x + w / 2) - w / 2, sy(y + h / 2) - h / 2
        else:
            x, w = sx(x), sx(x + w) - sx(x)
            y, h = sy(y), sy(y + h) - sy(y)
        ox, oy = origin[parent]
        origin[cid] = (x, y)
        cells.append(
            f'<mxCell id="{cid}" value={quoteattr(value)} style={quoteattr(style)} vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x - ox}" y="{y - oy}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )

    def edge(cid, src, tgt, style, value="", pts=()):
        pt = ""
        if pts:
            pt = (
                '<Array as="points">'
                + "".join(f'<mxPoint x="{sx(x)}" y="{sy(y)}"/>' for x, y in pts)
                + "</Array>"
            )
        cells.append(
            f'<mxCell id="{cid}" value={quoteattr(value)} style={quoteattr(style)} edge="1" parent="1" '
            f'source="{src}" target="{tgt}"><mxGeometry relative="1" as="geometry">{pt}</mxGeometry></mxCell>'
        )

    def free_edge(cid, x1, y1, x2, y2, style, raw=False):
        if not raw:
            x1, x2, y1, y2 = sx(x1), sx(x2), sy(y1), sy(y2)
        cells.append(
            f'<mxCell id="{cid}" value="" style={quoteattr(style)} edge="1" parent="1">'
            f'<mxGeometry relative="1" as="geometry"><mxPoint x="{x1}" y="{y1}" as="sourcePoint"/>'
            f'<mxPoint x="{x2}" y="{y2}" as="targetPoint"/></mxGeometry></mxCell>'
        )

    def lab(name, cap=None):
        if slide:
            name = SLIDE_NAMES.get(name, name)
        b = f'<b style="font-family:{DISPLAY}">{name}</b>'
        if not cap or slide:
            return b
        return f'{b}<br><span style="font-size:{F}px;font-weight:normal;color:{T["cap"]}">{cap}</span>'

    def group(owner=None, solid=False, sl=10):
        ln = T[owner] if owner else T["grp_ln"]
        fc = T[owner] if owner else T["txt"]
        fill = (
            f"fillColor={T[owner + '_tint']};gradientColor={T['bg']};gradientDirection=south;"
            if owner
            else "fillColor=none;"
        )
        dash = "" if solid else "dashed=1;dashPattern=6 4;"
        sw = 2 if owner else 1
        return (
            "rounded=1;absoluteArcSize=1;arcSize=10;whiteSpace=wrap;html=1;container=1;collapsible=0;"
            f"recursiveResize=0;{fill}strokeColor={ln};strokeWidth={sw};{dash}fontFamily={DISPLAY};fontSize={G};"
            f"fontStyle=1;fontColor={fc};align=left;verticalAlign=top;spacingLeft={sl};spacingTop=6;"
        )

    def service(sl=48):
        """An AWS service boundary: solid, neutral, so the boxes inside can show their owners."""
        return (
            "rounded=1;absoluteArcSize=1;arcSize=8;whiteSpace=wrap;html=1;container=1;collapsible=0;"
            f"recursiveResize=0;fillColor={T['bg']};strokeColor={T['grp_ln']};strokeWidth=1;fontFamily={DISPLAY};"
            f"fontSize={G};fontStyle=1;fontColor={T['txt']};align=left;verticalAlign=top;"
            f"spacingLeft={sl};spacingTop=8;"
        )

    def chip(owner=None, dashed=False, st=4, align="left", va="top"):
        fill = T[owner + "_tint"] if owner else T["box_fill"]
        if not owner and dark:  # the decks' frosted card
            fill = (
                "#2A4866;gradientColor=#0E1D2C;gradientDirection=south;fillOpacity=60"
            )
        ln = T[owner] if owner else T["box_ln"]
        d = "dashed=1;dashPattern=6 4;" if dashed else ""
        return (
            "rounded=1;absoluteArcSize=1;arcSize=8;whiteSpace=wrap;html=1;container=1;collapsible=0;"
            f"recursiveResize=0;fillColor={fill};strokeColor={ln};strokeWidth=1.5;{d}{FONT}fontSize={F};"
            f"fontColor={T['txt']};align={align};verticalAlign={va};spacingLeft=8;spacingRight=6;"
            f"spacingTop={st};"
        )

    def aws(res, fill):
        return (
            "sketch=0;outlineConnect=0;dashed=0;html=1;aspect=fixed;strokeColor=#ffffff;"
            f"fillColor={fill};shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{res};"
        )

    def text(align="center", va="middle", fc=None, fs=F):
        return (
            f"text;html=1;whiteSpace=wrap;strokeColor=none;fillColor=none;align={align};"
            f"verticalAlign={va};{FONT}fontSize={fs};fontColor={fc or T['txt']};spacing=0;"
        )

    def line(main=False, end=True, dashed=False):
        c = T["main"] if main else T["arrow"]
        d = "dashed=1;dashPattern=8 5;" if dashed else ""
        return (
            f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeColor={c};"
            f"strokeWidth={2.25 if main else 1.5};{d}{FONT}endArrow={'classic' if end else 'none'};"
            f"endFill=1;startArrow=none;fontSize={F};fontColor={T['txt']};"
            f"labelBackgroundColor={T['bg']};" + ("flowAnimation=1;" if main else "")
        )

    def icon(
        cid,
        parent,
        cx,
        cy,
        res,
        fill,
        title,
        cap=None,
        pos="below",
        w=180,
        s=46,
        lh=110,
    ):
        """An AWS icon centred on (cx, cy) with its label above, below, left or right."""
        vtx(cid, parent, (cx - s / 2, cy - s / 2, s, s), "", aws(res, fill))
        if pos == "above":
            box, st = (cx - w / 2, cy - s / 2 - 6 - lh, w, lh), text(va="bottom")
        elif pos == "below":
            box, st = (cx - w / 2, cy + s / 2 + 6, w, lh), text(va="top")
        elif pos == "left":
            box, st = (cx - s / 2 - 10 - w, cy - lh / 2, w, lh), text(align="right")
        else:
            box, st = (cx + s / 2 + 10, cy - lh / 2, w, lh), text(align="left")
        vtx(cid + "_t", parent, box, lab(title, cap), st)

    EX = "exitX={};exitY={};entryX={};entryY={};"
    Y1, Y2, YA = 340, 835, 700  # the event row, the SAP call row, the agent's row

    # ---------------- groups ----------------
    vtx("acc", "1", (330, 10, 1740, 1180), "AWS account", group(sl=12))
    vtx("sapc", "1", (0, 10, 300, 1170), "SAP system", group())
    vtx("sl", "1", (360, 60, 440, 1110), "SAP layer", group("sap"))
    vtx(
        "uc",
        "1",
        (840, 60, 1210, 480),
        "Use case: SAP exception handling"
        f' <span style="font-weight:normal;font-size:{F}px">(one of many)</span>',
        group("uc"),
    )
    vtx(
        "pl",
        "1",
        (840, 580, 1210, 590),
        "Platform: Amazon Bedrock AgentCore",
        group("plat"),
    )
    vtx("ppl", "1", (2110, 50, 310, 1130), "People and clients", group())
    if not slide:
        vtx(
            "sl_c",
            "sl",
            (382, 96, 400, 26),
            "Shared by every SAP use case",
            text(align="left", fc=T["sap"]),
        )

    # ---------------- SAP ----------------
    with open(os.path.join(HERE, "assets", "sap.png"), "rb") as f:
        sap_png = "data:image/png," + base64.b64encode(f.read()).decode()
    if dark:  # the logo needs a light tile on black
        vtx(
            "sap_tile",
            "sapc",
            (232, 16, 64, 48),
            "",
            "rounded=1;arcSize=12;fillColor=#FFFFFF;strokeColor=none;",
        )
    vtx(
        "sap_logo",
        "sapc",
        (236, 12, 56, 56),
        "",
        f"shape=image;html=1;aspect=fixed;imageAspect=0;image={sap_png};",
    )
    for cid, y, title, cap in (
        (
            "badi",
            98,
            "Optional: BAdI implementation",
            "Fires on posting; its IF mirrors the pack's filter",
        ),
        ("outbox", 198, "Outbox table", "One insert, in the posting's update task"),
        (
            "drain",
            298,
            "Drain job, every minute",
            "Sends with the AWS SDK for SAP ABAP",
        ),
    ):
        vtx(cid, "sapc", (20, y, 260, 84), lab(title, cap), chip(dashed=True, st=6))
    vtx(
        "odata",
        "sapc",
        (20, Y2 - 60, 260, 120),
        lab("OData services", "What the packs read, and the writes they make"),
        chip(st=8),
    )

    # ---------------- SAP layer ----------------
    icon(
        "bus",
        "sl",
        580,
        Y1,
        "eventbridge",
        "#e7157b",
        "sap-events bus",
        "Optional. Each system's sender role may put only its own source and its use cases' topics; "
        "the bus denies everyone else",
        pos="above",
        w=400,
        lh=150,
    )
    icon(
        "claims",
        "sl",
        470,
        640,
        "parameter_store",
        "#e7157b",
        "SAP access claims",
        "Which use case may call which system, and write",
        w=200,
    )
    icon(
        "creds",
        "sl",
        690,
        640,
        "secrets_manager",
        "#dd344c",
        "SAP credentials",
        "One service account per system",
        w=200,
    )
    icon(
        "mcp",
        "sl",
        580,
        Y2,
        "bedrock_agentcore",
        "#01a88d",
        "AWS for SAP MCP Server",
        "One per SAP system, on AgentCore Runtime",
        w=330,
    )

    vtx(
        "sl_lib",
        "sl",
        (375, 950, 410, 205 if slide else 150),
        (
            "<b>Code library</b><br>"
            + "<br>".join(
                "&bull; " + what
                for what in (
                    "SAP access claim, Cedar permits",
                    "Rule from the bus",
                    "Direct SAP access",
                    "OData client",
                )
            )
        )
        if slide
        else "<b>Code library (per SAP use case)</b><br>"
        + "<br>".join(
            "&bull; " + what
            for what in (
                "Its SAP access claim and Cedar permits",
                "A rule from the bus to its queue",
                "Direct SAP access for its functions",
                "The OData client they use",
            )
        ),
        chip("sap", st=8),
    )

    # ---------------- use case ----------------
    icon(
        "packs",
        "uc",
        885,
        145,
        "s3",
        "#7aa116",
        "Use case packs",
        "Each pack's SAP query, prompt, procedures and limits; every edit is a version",
        pos="right",
        w=330,
        lh=90,
    )
    icon(
        "kb",
        "uc",
        1330,
        145,
        "bedrock",
        "#01a88d",
        "Amazon Bedrock Knowledge Bases",
        "Optional: search over procedures and SAP API docs",
        pos="right",
        w=255,
        lh=112,
    )
    MAIN = (  # id, x, resource, fill, title, caption
        ("rule", 920, "eventbridge", "#e7157b", "Rule", "Routes this use case's topic"),
        (
            "ingest",
            1110,
            "sqs",
            "#e7157b",
            "Ingest queue",
            "Encrypted; only the rule and the push endpoint may send",
        ),
        (
            "consumer",
            1300,
            "lambda",
            "#ed7100",
            "Ingest consumer",
            "Re-reads SAP for the event's keys",
        ),
        (
            "cases",
            1490,
            "dynamodb",
            "#c925d1",
            "Cases",
            "One per SAP document: the id comes from its key",
        ),
        (
            "fifo",
            1690,
            "sqs",
            "#e7157b",
            "Agent queue",
            "FIFO; auto queues new cases, manual waits",
        ),
        (
            "inv",
            1890,
            "lambda",
            "#ed7100",
            "Agent invoker",
            "Writes a run grant, starts one run per case",
        ),
    )
    for cid, x, res, fill, title, cap in MAIN:
        icon(
            cid,
            "uc",
            x,
            Y1,
            res,
            fill,
            title,
            cap,
            pos="below" if cid == "fifo" else "above",
            w=150 if cid == "rule" else 180,
        )
    icon(
        "erp",
        "uc",
        1110,
        440,
        "api_gateway",
        "#8c4fff",
        "SAP push endpoint",
        "Optional: SAP posts over HTTPS",
        pos="right",
        w=150,
        lh=80,
    )
    icon(
        "poller",
        "uc",
        1490,
        440,
        "lambda",
        "#ed7100",
        "SAP poller",
        "The default: reads SAP every 15 min",
        pos="left",
        w=150,
        lh=100,
    )
    vtx(
        "con",
        "uc",
        (1645, 86, 390, 118),
        "Business Operations console",
        chip("uc", dashed=True, st=4),
    )
    for i, (cid, res, fill, name) in enumerate(
        (
            ("cf", "cloudfront", "#8c4fff", "Console"),
            ("api", "api_gateway", "#8c4fff", "REST API"),
            ("appsync", "appsync", "#e7157b", "Run channel"),
        )
    ):
        cx = 1712 + 128 * i
        vtx(cid, "con", (cx - 18, 122, 36, 36), "", aws(res, fill))
        if not slide:
            vtx(cid + "_t", "con", (cx - 62, 162, 124, 30), name, text(va="top"))

    icon(
        "exemplar",
        "uc",
        1972,
        395,
        "lambda",
        "#ed7100",
        "Exemplars",
        "Daily: builds examples from rated cases",
        pos="below",
        w=150,
        lh=90,
    )

    # ---------------- platform ----------------
    vtx("gw", "pl", (860, 620, 740, 330), "AgentCore Gateway", service())
    vtx("gw_i", "gw", (872, 628, 28, 28), "", aws("bedrock_agentcore", "#01a88d"))
    vtx(
        "chk",
        "gw",
        (875, 668, 715, 64),
        "",
        "rounded=0;fillColor=none;strokeColor=none;container=1;",
    )
    for cid, x, owner, title, cap in (
        ("cedar", 880, "plat", "Cedar policy engine", "Permits; off by default"),
        ("runchecks", 1120, "uc", "Run checks", "In the interceptor"),
        ("claimchk", 1360, "sap", "SAP access claim check", "In the interceptor"),
    ):
        vtx(
            cid,
            "chk",
            (x, YA - 28, 230, 56),
            lab(title, cap),
            chip(owner, st=4, align="center", va="middle"),
        )
    TARGETS = (  # id, x, owner, title, caption
        ("t_sap", 880, "sap", "SAP target", "The SAP layer's"),
        ("t_case", 1040, "uc", "Case tools", "DynamoDB"),
        ("t_sop", 1178, "uc", "Procedure tool", "The case's procedure"),
        ("t_note", 1316, "uc", "Notification", "Amazon SES or tickets"),
        ("t_kb", 1454, "uc", "Knowledge", "Optional"),
    )
    for cid, x, owner, title, cap in TARGETS:
        w = 150 if cid == "t_sap" else 128
        vtx(cid, "gw", (x, Y2 - 65, w, 130), lab(title, cap), chip(owner, st=8))
    # Optional (`gateway.people`): a second Gateway for people's remote MCP clients. It has no
    # arrow: one from the clients would cross the Runtime or the services row.
    vtx(
        "pgw",
        "pl",
        (860, 965, 650, 115),
        lab(
            "People Gateway",
            "Optional: AWS Quick and Copilot Studio, each person signed in as themselves",
        ),
        chip("plat", dashed=True, st=8),
    )
    vtx("rt", "pl", (1640, 620, 395, 468), "AgentCore Runtime", service(sl=40))
    vtx("rt_i", "rt", (1648, 628, 28, 28), "", aws("bedrock_agentcore", "#01a88d"))
    vtx(
        "agent_box",
        "rt",
        (1655, 662, 365, 413),
        "",
        chip("uc", dashed=True),
    )
    vtx(
        "agent",
        "agent_box",
        (1672, YA - 22, 44, 44),
        "",
        "sketch=0;html=1;aspect=fixed;pointerEvents=1;shape=mxgraph.aws4.agent2;fillColor=#01a88d;strokeColor=none;",
    )
    vtx(
        "agent_t",
        "agent_box",
        (1726, YA - 30, 290, 84),
        lab(
            "Business process agent",
            "The reasoning loop, on Strands Agents",
        ),
        text(align="left"),
    )
    vtx(
        "route",
        "agent_box",
        (1670, 780, 335, 285),
        "Process-type routing",
        chip("uc", st=6),
    )
    vtx(
        "pack",
        "route",
        (1688, 830, 40, 48),
        "",
        f"shape=cube;darkOpacity=0.2;html=1;fillColor={T['uc']};strokeColor=none;size=8;",
    )
    vtx(
        "pack_t",
        "route",
        (1742, 818, 255, 70),
        lab("Use case pack", "SAP query, prompt, limits, case document check"),
        text(align="left"),
    )
    for i in range(3):
        vtx(
            f"doc{i}",
            "route",
            (1684 + 9 * i, 904 + 7 * i, 32, 38),
            "",
            f"shape=document;html=1;boundedLbl=1;fillColor={T['uc']};strokeColor={T['bg']};size=0.2;",
        )
    edge("e_docs", "pack", "doc0", line() + EX.format(0.5, 1, 0.5, 0))
    vtx(
        "docs_t",
        "route",
        (1742, 896, 255, 100),
        lab("Standard operating procedures", "Versioned, one per process type"),
        text(align="left"),
    )
    vtx(
        "route_eg",
        "route",
        (1684, 995, 315, 63),
        "<i>For example: the AP three-way match exceptions pack</i>",
        text(align="left", fs=F if slide else F - 2),
    )
    edge("e_route", "agent", "route", line() + EX.format(0.5, 1, 0.1475, 0))
    for i, (title, cap, res, fill) in enumerate(
        (
            ("AgentCore Identity", "Token vault", "bedrock_agentcore", "#01a88d"),
            ("AgentCore Memory", "Each run's history", "bedrock_agentcore", "#01a88d"),
            (
                "AgentCore Observability",
                "Traces and logs",
                "bedrock_agentcore",
                "#01a88d",
            ),
            ("Amazon Cognito", "Federates your IdP", "cognito", "#dd344c"),
            ("Amazon Bedrock", "Models and guardrails", "bedrock", "#01a88d"),
        )
    ):
        x = 865 + 235 * i
        vtx(f"ps{i}", "pl", (x, 1108, 36, 36), "", aws(res, fill))
        vtx(
            f"ps{i}_t",
            "pl",
            (x + 44, 1098, 186, 58),
            lab(title, cap),
            text(align="left"),
        )

    # ---------------- people ----------------
    vtx(
        "user",
        "ppl",
        (2130, 129, 32, 32),
        "",
        f"sketch=0;html=1;aspect=fixed;pointerEvents=1;shape=mxgraph.aws4.authenticated_user;"
        f"fillColor={T['user']};strokeColor=none;",
    )
    vtx(
        "user_t",
        "ppl",
        (2172, 101, 240, 88),
        lab("Process owners and approvers", "In the console"),
        text(align="left"),
    )
    vtx(
        "idp",
        "ppl",
        (2125, 610, 280, 110),
        lab("Identity provider", "Entra ID, Okta or Ping; Cognito federates it"),
        chip(st=8),
    )
    vtx(
        "mcpc",
        "ppl",
        (2125, 1000, 280, 130),
        lab(
            "MCP clients",
            "Kiro, Claude Code. Optional: AWS Quick and Copilot Studio, through the people Gateway",
        ),
        chip(st=8),
    )

    # ---------------- edges ----------------
    M = line(True)
    edge("e_badi", "badi", "outbox", line() + EX.format(0.5, 1, 0.5, 0))
    edge("e_outbox", "outbox", "drain", line() + EX.format(0.5, 1, 0.5, 0))
    P = line(dashed=True)  # the optional push path
    edge("e_drain", "drain", "bus", P + EX.format(1, 0.5, 0, 0.5))
    edge("e_bus", "bus", "rule", P + EX.format(1, 0.5, 0, 0.5))
    for a, b in itertools.pairwise(MAIN):
        push = a[0] in ("rule", "ingest", "consumer")
        edge(f"e_{a[0]}", a[0], b[0], (P if push else M) + EX.format(1, 0.5, 0, 0.5))
    edge("e_inv", "inv", "agent_box", M + EX.format(0.5, 1, 0.6438, 0))
    edge(
        "e_agent",
        "agent_box",
        "claimchk",
        M + "exitX=0;exitY=0.095;entryX=1;entryY=.5;",
    )
    edge("e_chk1", "claimchk", "runchecks", M + EX.format(0, 0.5, 1, 0.5))
    edge("e_chk2", "runchecks", "cedar", M + EX.format(0, 0.5, 1, 0.5))
    for cid, x, *_ in TARGETS:
        edge(
            f"e_{cid}",
            "chk",
            cid,
            M
            + EX.format(
                round((x + (75 if cid == "t_sap" else 64) - 875) / 715, 4), 1, 0.5, 0
            ),
        )
    edge("e_tsap", "t_sap", "mcp", M + EX.format(0, 0.5, 1, 0.5))
    edge("e_mcp", "mcp", "odata", M + EX.format(0, 0.5, 1, 0.5))
    edge("e_erp", "erp", "ingest", P + EX.format(0.5, 0, 0.5, 1))
    edge("e_poll", "poller", "cases", M + EX.format(0.5, 0, 0.5, 1))
    edge(
        "e_direct",
        "poller",
        "odata",
        line(dashed=True) + EX.format(0.5, 1, 0.5, 0),
        "The poller reads SAP directly, as the service account (the consumer too, for a pushed event)",
        pts=((1490, 558), (150, 558)),
    )
    edge("e_con", "con", "fifo", line() + "exitX=0.1154;exitY=1;entryX=.5;entryY=0;")
    edge("e_user", "user", "con", line() + EX.format(0, 0.5, 1, 0.5))
    edge(
        "e_mcpc",
        "mcpc",
        "gw",
        line() + EX.format(0, 0.5, 0.9054, 1),
        "MCP",
        pts=((1530, 1085),),
    )

    # ---------------- step badges ----------------
    def badge(person=False, d=32):
        fill = (
            T["step2"]
            if person
            else f"{T['step']};gradientColor={T['step2']};gradientDirection=east"
        )
        return (
            f"ellipse;html=1;aspect=fixed;fillColor={fill};strokeColor=none;fontFamily={DISPLAY};"
            f"fontColor=#FFFFFF;fontStyle=1;fontSize={min(F, d - 8)};"
        )

    for n, (cx, cy) in enumerate(
        ((1490, 392), (1590, Y1), (1890, 560), (1620, YA), (820, Y2), (1690, 260)), 1
    ):
        d = 42 if slide else 32
        vtx(
            f"b{n}",
            "1",
            (cx - d / 2, cy - d / 2, d, d),
            str(n),
            badge(person=n == 6, d=d),
        )

    # ---------------- legend ----------------
    W = round(sx(2420))
    LY = sy(1225)
    nobg = lambda st: st.replace(f"labelBackgroundColor={T['bg']};", "")
    box_sw = (
        "rounded=1;absoluteArcSize=1;arcSize=6;fillColor=none;"
        + f"strokeColor={T['grp_ln']};strokeWidth=1;"
    )
    swatches = (
        ("Main path", "main", None),
        ("Optional: SAP pushes events", "push", None),
        ("Control or data flow", "flow", None),
        ("AWS service boundary", "service", None),
        ("Logical group", "group", None),
        ("Platform", "chip", "plat"),
        ("SAP layer", "chip", "sap"),
        ("Use case", "chip", "uc"),
    )

    def swatch(k, kind, owner, x, y):
        if kind in ("main", "flow", "push"):
            st = line(dashed=True) if kind == "push" else line(kind == "main")
            free_edge(f"lg{k}", x, y, x + 44, y, nobg(st), raw=True)
        else:
            st = {"service": box_sw, "group": box_sw + "dashed=1;dashPattern=6 4;"}.get(
                kind
            ) or chip(owner)
            vtx(f"lg{k}", "1", (x, y - 9, 40, 18), "", st, raw=True)

    est = lambda t: len(t) * F * 0.58  # a generous text width
    row_h = F + 16
    x, y = 0, LY
    for k, (name, kind, owner) in enumerate(swatches):
        swatch(k, kind, owner, x, y)
        w = est(name) + 10
        label = f"<b>{name}</b>" if owner else name
        vtx(
            f"lg{k}_t",
            "1",
            (x + 52, y - row_h / 2, w, row_h),
            label,
            text(align="left", fc=T[owner] if owner else T["cap"]),
            raw=True,
        )
        x += 52 + w + 34
    steps = (
        "The poller finds the exception in SAP",
        "One case per SAP document",
        "One agent run per case",
        "Per-call authorization",
        "SAP reads and writes via the MCP server",
        "Manual start, or approval above the limit",
    )
    x, y = 0, y + row_h + 6
    for n, step in enumerate(steps, 1):
        w = est(step) + 10
        if x + 40 + w > W:
            x, y = 0, y + row_h + 4
        vtx(
            f"lgn{n}",
            "1",
            (x, y - 13, 26, 26),
            str(n),
            badge(person=n == 6, d=26),
            raw=True,
        )
        vtx(
            f"lgn{n}_t",
            "1",
            (x + 36, y - row_h / 2, w, row_h),
            step,
            text(align="left", fc=T["cap"]),
            raw=True,
        )
        x += 36 + w + 30

    H = round(y + row_h)
    cells.insert(
        0,
        f'<mxCell id="bg" value="" style="rounded=0;fillColor={T["bg"]};strokeColor=none;" '
        f'vertex="1" parent="1"><mxGeometry x="-10" y="-10" width="{W + 20}" height="{H + 20}" '
        'as="geometry"/></mxCell>',
    )
    return (
        f'<mxfile host="drawio"><diagram name="Architecture ({theme})" id="architecture-{theme}">'
        f'<mxGraphModel dx="1200" dy="700" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" '
        f'arrows="1" fold="1" page="1" pageScale="1" pageWidth="{W}" pageHeight="{H}" math="0" shadow="0" '
        f'background="{T["bg"]}"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        + "".join(cells)
        + "</root></mxGraphModel></diagram></mxfile>"
    )


def main():
    drawio = shutil.which("drawio") or sys.exit(
        "drawio (the draw.io desktop CLI) is not on PATH"
    )
    for theme in sys.argv[1:] or (*THEMES, "slide"):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, f"architecture.{theme}.drawio")
            with open(src, "w") as f:
                f.write(build(theme))
            out = os.path.join(HERE, f"architecture.{theme}.drawio.png")
            subprocess.run(
                [
                    drawio,
                    "-x",
                    "-f",
                    "png",
                    "-e",
                    "-s",
                    "1.5",
                    "-b",
                    "10",
                    "-o",
                    out,
                    src,
                ],
                check=True,
                capture_output=True,
            )
        print("wrote", os.path.relpath(out))


if __name__ == "__main__":
    main()
