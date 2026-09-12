import json
import sqlite3
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.dashboard import day_details, heatmap, latest_reflections, module_tree
from app.services.learning_clock import local_today
from app.services.llm_config import MODULES, get_module_config, provider_status
from app.services.review import exposed_recently
from app.services.source_coverage import coverage
from app.services.source_refresh import source_status
from app.storage.dependencies import get_database

router = APIRouter()
Database = Annotated[sqlite3.Connection, Depends(get_database)]


def page_context(request: Request, **values):
    return {"request": request, 'page_day': local_today().isoformat(), **values}


def configure_pages(templates: Jinja2Templates) -> APIRouter:
    @router.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, database: Database):
        modules = [get_module_config(database, module) for module in MODULES]
        return templates.TemplateResponse(request=request, name="settings.html",
                                          context=page_context(request, modules=modules, credentials=provider_status()))

    @router.get("/", response_class=HTMLResponse)
    def today_page(request: Request, database: Database):
        today = local_today()
        plans = database.execute(
            "SELECT p.*, COUNT(t.id) AS assigned, "
            "SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) AS completed "
            "FROM daily_plan p LEFT JOIN task t ON t.plan_id=p.id AND t.status!='cancelled' "
            "GROUP BY p.id ORDER BY p.plan_date DESC LIMIT 7"
        ).fetchall()
        tasks = database.execute(
            "SELECT t.*, v.prompt, q.question_type FROM task t "
            "JOIN question q ON q.id=t.question_id "
            "JOIN question_version v ON v.id=t.question_version_id "
            "WHERE (t.plan_id=(SELECT id FROM daily_plan WHERE plan_date=?) "
            "OR t.status IN ('pending','in_progress')) AND t.status!='cancelled' "
            "ORDER BY t.created_at, t.id",
            (today.isoformat(),),
        ).fetchall()
        return templates.TemplateResponse(
            request=request,
            name="today.html",
            context=page_context(request, plans=plans, tasks=tasks, today=today,
                                 reflection=latest_reflections(database, today),
                                 modules=module_tree(database), heatmap=heatmap(database, today), sources=source_status(database),
                                 allocation=next((json.loads(plan["allocation_json"]) for plan in plans if plan["plan_date"] == today.isoformat()), {}),
                                 current_plan=next((dict(plan) for plan in plans if plan["plan_date"] == today.isoformat()), None)),
        )

    @router.get("/days/{day}", response_class=HTMLResponse)
    def day_page(day: date, request: Request, database: Database):
        return templates.TemplateResponse(request=request, name="day.html", context=page_context(request, **day_details(database, day)))

    @router.get("/practice/{task_id}", response_class=HTMLResponse)
    def practice_page(task_id: str, request: Request, database: Database):
        task = database.execute(
            "SELECT t.*, q.question_type, v.prompt, v.reference_text, v.material_status, v.category_path "
            "FROM task t JOIN question q ON q.id=t.question_id "
            "JOIN question_version v ON v.id=t.question_version_id WHERE t.id=?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        resource = database.execute("SELECT materials_json FROM version_resources WHERE version_id=?",
                                    (task["question_version_id"],)).fetchone()
        materials = json.loads(resource[0]) if resource else []
        latest = database.execute("SELECT * FROM attempt WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                                  (task_id,)).fetchone()
        job = database.execute("SELECT j.* FROM model_job j JOIN evaluation_job_target target ON target.job_id=j.id "
                               "JOIN attempt a ON a.id=target.attempt_id WHERE a.task_id=? "
                               "ORDER BY a.rowid DESC,j.rowid DESC LIMIT 1", (task_id,)).fetchone()
        return templates.TemplateResponse(
            request=request,
            name="practice.html",
            context=page_context(request, task=task, materials=materials, latest=latest, job=job),
        )

    @router.get("/free-practice", response_class=HTMLResponse)
    def free_page(request: Request, database: Database):
        plans = database.execute(
            "SELECT id, plan_date FROM daily_plan ORDER BY plan_date DESC LIMIT 7"
        ).fetchall()
        return templates.TemplateResponse(
            request=request,
            name="free_practice.html",
            context=page_context(request, plans=plans, tasks=database.execute("SELECT t.id,t.status,v.prompt,v.category_path FROM task t JOIN question_version v ON v.id=t.question_version_id WHERE t.origin='free_practice' AND t.status IN ('pending','in_progress') ORDER BY t.rowid DESC").fetchall()),
        )

    @router.get("/interview", response_class=HTMLResponse)
    def interview_page(request: Request, database: Database):
        sessions = database.execute(
            "SELECT s.*, v.prompt, COUNT(t.id) AS turn_count FROM interview_session s "
            "JOIN question_version v ON v.id=s.question_version_id "
            "LEFT JOIN interview_turn t ON t.session_id=s.id "
            "GROUP BY s.id ORDER BY s.created_at DESC"
        ).fetchall()
        tasks = database.execute("SELECT q.id,v.prompt,v.category_path FROM question q JOIN question_version v ON v.id=q.current_version_id "
                                 "WHERE q.source_status='active' AND v.material_status IN ('complete','verified','text_complete') ORDER BY v.category_path,v.prompt").fetchall()
        return templates.TemplateResponse(
            request=request,
            name="interview.html",
            context=page_context(request, sessions=sessions, tasks=tasks),
        )

    @router.get("/interview/{session_id}", response_class=HTMLResponse)
    def interview_session_page(session_id: str, request: Request, database: Database):
        session = database.execute("SELECT s.*,v.prompt FROM interview_session s JOIN question_version v "
                                   "ON v.id=s.question_version_id WHERE s.id=?", (session_id,)).fetchone()
        if session is None:
            raise HTTPException(404, "面试会话不存在")
        turns = database.execute("SELECT * FROM interview_turn WHERE session_id=? ORDER BY rowid", (session_id,)).fetchall()
        dialogue = database.execute("SELECT t.* FROM interview_turn t WHERE t.session_id=? AND NOT EXISTS "
            "(SELECT 1 FROM model_job j WHERE j.result_id=t.id AND j.purpose='interview_feedback') "
            "ORDER BY t.rowid", (session_id,)).fetchall()
        latest_question = None
        if dialogue and session["status"] == "active" and database.execute("SELECT 1 FROM model_job WHERE result_id=? "
                                                                       "AND purpose='interview_followup'", (dialogue[-1]["id"],)).fetchone():
            latest_question = dialogue[-1]["content"]
        return templates.TemplateResponse(request=request, name="interview_session.html",
                                          context=page_context(request, session=session, turns=turns, latest_question=latest_question))

    @router.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, database: Database):
        sources = database.execute("SELECT * FROM source ORDER BY id").fetchall()
        candidate_rows = database.execute(
            "SELECT * FROM parser_candidate WHERE status='pending' ORDER BY id LIMIT 100"
        ).fetchall()
        candidates = [
            {**dict(row), "draft": json.loads(row["draft_json"])} for row in candidate_rows
        ]
        material_errors = database.execute("SELECT source_id,last_error,COUNT(*) AS count FROM source_dependency "
                                           "WHERE status='pending' GROUP BY source_id,last_error").fetchall()
        return templates.TemplateResponse(
            request=request,
            name="sources.html",
            context=page_context(request, sources=sources, candidates=candidates, material_errors=material_errors,
                                 theory_modules=module_tree(database), code_modules=module_tree(database, 'code'),
                                 coverage={source["id"]: coverage(database, source["id"]) for source in sources if source["question_type"] == "code"}),
        )

    @router.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, database: Database):
        rounds = database.execute(
            "SELECT r.*, q.question_type, v.prompt, v.category_path, COUNT(p.id) AS valid_pass_count, "
            "MIN(p.submitted_at) AS first_pass_at, MAX(p.submitted_at) AS last_pass_at "
            "FROM review_round r JOIN question q ON q.id=r.question_id "
            "JOIN question_version v ON v.id=(SELECT v2.id FROM question_version v2 "
            "WHERE v2.question_id=q.id AND v2.review_basis_id=r.review_basis_id ORDER BY v2.rowid DESC LIMIT 1) "
            "LEFT JOIN valid_review_pass p ON p.review_round_id=r.id "
            "GROUP BY r.id ORDER BY CASE WHEN r.status='active' THEN 0 ELSE 1 END,r.started_at DESC"
        ).fetchall()
        rounds = [dict(row) for row in rounds]
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        for item in rounds:
            if database.execute('SELECT 1 FROM valid_review_pass WHERE review_round_id=? AND activity_date=?', (item['id'], now.date().isoformat())).fetchone():
                item['hint'] = '今天已记过一次独立答对，可以继续练，但不会重复计数。'
            elif exposed_recently(database, item['question_id'], now):
                item['hint'] = '最近24小时查看过答案、历史作答或模型复盘；现在可以练，独立答对次数暂不增加。'
            else:
                item['hint'] = '先独立作答，通过后记录今天的一次进步。'
        return templates.TemplateResponse(
            request=request,
            name="review.html",
            context=page_context(request, rounds=rounds),
        )

    @router.get("/history", response_class=HTMLResponse)
    def history_page(request: Request, database: Database):
        attempts = database.execute(
            "SELECT a.*, t.question_id, t.origin, v.prompt FROM attempt a "
            "JOIN task t ON t.id=a.task_id "
            "JOIN question_version v ON v.id=a.question_version_id "
            "WHERE a.submitted_at IS NOT NULL ORDER BY a.submitted_at DESC LIMIT 100"
        ).fetchall()
        reflections = database.execute(
            "SELECT * FROM reflection ORDER BY activity_date DESC, version DESC LIMIT 100"
        ).fetchall()
        return templates.TemplateResponse(
            request=request,
            name="history.html",
            context=page_context(request, attempts=attempts, reflections=reflections, today=datetime.now(ZoneInfo("Asia/Shanghai")).date()),
        )

    return router
