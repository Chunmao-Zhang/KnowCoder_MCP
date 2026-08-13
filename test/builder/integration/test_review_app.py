from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

from knowcoder_workspace_builder.review.page import write_review_page
from knowcoder_workspace_builder.review.service import ReviewService
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.sessions import BuildStateStore


def _problem_gate(state):
    state.problem = {
        "question": state.question,
        "scope": {},
        "steps": ["Compare every requested record and retain its source."],
        "missing_information": [],
    }
    state.status = "needs_problem_confirmation"
    return state


def _problem_page(runtime_project: Path) -> tuple[Path, object]:
    layout = ProjectLayout(runtime_project)
    states = BuildStateStore(layout)
    initial = states.create("Compare the supplied records.", [], session_id="session-review-app-1234")
    gate = states.update(initial.session_id, initial.version, _problem_gate)
    review = ReviewService(layout).get(gate.session_id, "problem")
    return write_review_page(layout, gate.session_id, "problem", gate.version, review), gate


def test_problem_review_is_a_self_contained_workspace_file(runtime_project: Path) -> None:
    page, gate = _problem_page(runtime_project)

    assert page == runtime_project / ".knowcoder_workspace/sessions/session-review-app-1234/workspace/review" / f"problem-v{gate.version}.html"
    content = page.read_text(encoding="utf-8")
    assert "Problem Review · KnowCoder" in content
    assert "Compare the supplied records." in content
    assert "Compare every requested record" in content
    assert "<style>" in content
    assert "<script" not in content
    assert "fetch(" not in content
    assert "/api/" not in content


def test_review_file_uri_survives_without_a_review_server(runtime_project: Path) -> None:
    page, _gate = _problem_page(runtime_project)
    uri = page.resolve(strict=True).as_uri()
    parsed = urlsplit(uri)

    assert parsed.scheme == "file"
    assert Path(unquote(parsed.path)).read_text(encoding="utf-8") == page.read_text(encoding="utf-8")


def test_problem_review_browser_renders_content_without_controls(runtime_project: Path) -> None:
    page_path, _gate = _problem_page(runtime_project)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(page_path.resolve(strict=True).as_uri(), wait_until="load")
            assert page.locator(".step-row").count() == 1
            assert "Compare every requested record" in page.locator(".step-row").inner_text()
            assert page.locator("button, input, textarea").count() == 0
            assert "Continue in your Agent conversation" not in page.locator("body").inner_text()
        finally:
            browser.close()


def test_problem_review_timeline_and_content_share_step_center(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-review-timeline-1234", create=True)
    page_path = write_review_page(
        layout,
        paths.session_id,
        "problem",
        1,
        {
            "question": "Compare the evidence.",
            "workflow_steps": [
                {"description": "Collect primary evidence from official sources."},
                {"description": "Compare the evidence and document gaps."},
            ],
        },
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(page_path.resolve(strict=True).as_uri(), wait_until="load")
            geometry = page.locator(".step-row").first.evaluate(
                """row => {
                    const marker = row.querySelector('.step-index').getBoundingClientRect();
                    const content = row.querySelector('.step-content').getBoundingClientRect();
                    const track = row.querySelector('.step-track');
                    const line = getComputedStyle(track, '::after');
                    const trackRect = track.getBoundingClientRect();
                    return {
                        markerCenterX: marker.left + marker.width / 2,
                        markerCenterY: marker.top + marker.height / 2,
                        contentCenterY: content.top + content.height / 2,
                        lineCenterX: trackRect.left + parseFloat(line.left),
                        contentBackground: getComputedStyle(row.querySelector('.step-content')).backgroundImage,
                    };
                }"""
            )
            assert abs(geometry["markerCenterX"] - geometry["lineCenterX"]) < 0.1
            assert abs(geometry["markerCenterY"] - geometry["contentCenterY"]) < 0.1
            assert geometry["contentBackground"] != "none"
        finally:
            browser.close()


def test_review_versions_are_immutable(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-review-version-1234", create=True)
    first = write_review_page(
        layout,
        paths.session_id,
        "problem",
        2,
        {"question": "Original question", "workflow_steps": [{"description": "Original step"}]},
    )
    second = write_review_page(
        layout,
        paths.session_id,
        "problem",
        4,
        {"question": "Revised question", "workflow_steps": [{"description": "Revised step"}]},
    )

    assert first != second
    assert "Original question" in first.read_text(encoding="utf-8")
    assert "Revised question" in second.read_text(encoding="utf-8")

    reused = write_review_page(
        layout,
        paths.session_id,
        "problem",
        2,
        {"question": "Conflicting question", "workflow_steps": [{"description": "Conflicting step"}]},
    )

    assert reused == first
    assert "Original question" in reused.read_text(encoding="utf-8")
    assert "Conflicting question" not in reused.read_text(encoding="utf-8")


def test_concurrent_review_writers_reuse_one_snapshot(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-review-concurrent-1234", create=True)

    def write(index: int) -> Path:
        return write_review_page(
            layout,
            paths.session_id,
            "problem",
            3,
            {
                "question": f"Concurrent renderer {index}",
                "workflow_steps": [{"description": "Accepted step"}],
            },
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))

    assert len(set(results)) == 1
    content = results[0].read_text(encoding="utf-8")
    assert sum(f"Concurrent renderer {index}" in content for index in range(4)) == 1
