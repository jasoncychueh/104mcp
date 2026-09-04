"""Behaviour tests for the two asset tools (tools/resume_files.py).

No test here opens a browser or touches the network: both transports
(`fetch` for the résumé-detail sub-request, `fetch_asset` for the file) are
substituted with a scripted spy, exactly as the existing tool tests do.

EVERY byte, name, filename, phone number and e-mail below is synthetic and
obviously so. The file headers are hand-built signatures plus filler; the
"104 filename" values are strings like SYNTHETIC-CANDIDATE-NAME.pdf, chosen
so a leak into a landed filename or a return value is unmistakable.
"""

import json
import logging
import os
import time
from pathlib import Path

import pytest
import pytest_asyncio

from mcp104.browser.api_client import RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.db.database import Database
from mcp104.tools.helpers import get_session_id
import mcp104.tools.resume_files as resume_files_mod
from mcp104.tools.resume_files import (
    RESUME_FILE_RETENTION_SECONDS,
    register_resume_file_tools,
    sweep_expired_files,
    write_asset_atomically,
)

CANDIDATE_ID = "1728037773409"  # 13 digits, the measured idNo shape; not a real candidate

_SYNTH_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
_SYNTH_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_SYNTH_PDF = b"%PDF-1.4\n% synthetic\n" + b"\x00" * 32
_SYNTH_ZIP = b"PK\x03\x04" + b"\x00" * 32
_NOT_AUTH_HTML = (
    '<html><head><script>location.href="https://vip.104.com.tw/";</script>'
    "</head><body></body></html>"
)

_PHOTO_URL = "https://asset.vip.104.com.tw/download/webHeadShot?v=SYNTHETIC-TOKEN-AAA"
_ATTACH_URL = "https://asset.vip.104.com.tw/download/resumeAttach/SYNTHETIC-TOKEN-BBB"
_PLACEHOLDER_URL = "https://static.104.com.tw/104main/vipphp/no_photo.png"


# ── fake transport plumbing ───────────────────────────────────────────────

class _Spy:
    """One script for both transports, consumed in call order, so a test can
    assert exactly how many 104 requests were issued and in what order."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list[tuple[str, str]] = []

    def _next(self):
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def fetch(self, endpoint, *, cookie_header, params=None, body=None):
        self.calls.append(("json", endpoint.key))
        return self._next()

    async def fetch_asset(self, route, url, *, cookie_header):
        self.calls.append(("asset", route.key))
        return self._next()


def _patch_transports(monkeypatch, spy) -> None:
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy.fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy.fetch, raising=False)
    monkeypatch.setattr("mcp104.browser.api_client.fetch_asset", spy.fetch_asset)
    monkeypatch.setattr("mcp104.tools.helpers.fetch_asset", spy.fetch_asset, raising=False)


def _never_called(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise AssertionError("no request may be issued in this case")
    monkeypatch.setattr("mcp104.browser.api_client.fetch", boom)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", boom, raising=False)
    monkeypatch.setattr("mcp104.browser.api_client.fetch_asset", boom)
    monkeypatch.setattr("mcp104.tools.helpers.fetch_asset", boom, raising=False)


def _json_resp(body: dict, status: int = 200) -> RawResponse:
    text = json.dumps(body, ensure_ascii=False)
    return RawResponse(status=status, location=None, content_type="application/json",
                       body=text, body_bytes=text.encode("utf-8"), parsed_json=body)


def _asset_resp(status=200, *, body_bytes=b"", content_type=None, location=None) -> RawResponse:
    from mcp104.browser.api_client import detect_magic
    body = "" if detect_magic(body_bytes) is not None else body_bytes[:4096].decode("utf-8", errors="replace")
    return RawResponse(status=status, location=location, content_type=content_type,
                       body=body, body_bytes=body_bytes, parsed_json=None)


def _detail(*, personal_pic=None, attachments=None, browse_limit=None, extra_resume=None) -> RawResponse:
    """A résumé-detail envelope. `browseLimit` is a sibling of `resume`
    UNDER `data`, which is where 104 actually puts it."""
    # A non-empty `resume` even when the case under test wants no
    # personalPic: classify()'s family-B floor requires a truthy `resume`,
    # so an empty one would fail transport-level classification instead of
    # exercising this tool.
    resume: dict = {"idNo": CANDIDATE_ID}
    if personal_pic is not None:
        resume["personalPic"] = personal_pic
    if attachments is not None:
        resume["attachArr"] = attachments
    if extra_resume:
        resume.update(extra_resume)
    data: dict = {"resume": resume}
    if browse_limit is not None:
        data["browseLimit"] = browse_limit
    return _json_resp({"data": data, "metadata": {}})


def _attachment(sort, *, title="SYNTHETIC-TITLE", link=_ATTACH_URL,
                filename="SYNTHETIC-CANDIDATE-NAME.pdf", type_=1, preview=None):
    return {"sort": sort, "title": title, "link": link, "filename": filename,
            "type": type_, "preview": preview}


class FakeSessionObj:
    pass


class FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeApp:
    def __init__(self, session_pool, database):
        self.session_pool = session_pool
        self.config = get_config()
        self.db = database


class FakeCtx:
    def __init__(self, session_pool, database):
        self.session = FakeSessionObj()
        self.request_context = FakeRequestContext(FakeApp(session_pool, database))


class FakeMCP:
    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[kwargs.get("name", fn.__name__)] = fn
            return fn
        return decorator


def _tools():
    mcp = FakeMCP()
    register_resume_file_tools(mcp)
    return mcp.tools


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


# aiosqlite's Connection SUBCLASSES threading.Thread and never sets daemon,
# so every open connection parks a non-daemon thread forever on its queue.
# threading._shutdown() joins all of them at interpreter exit — an unclosed
# one therefore hangs the process AFTER pytest has already printed a green
# summary, which is exactly why the symptom reads as "the suite passed and
# then never returned". tests/test_search.py and tests/test_messaging.py
# already track and drain the same way; _new_session is a plain helper
# called from inside test bodies rather than a fixture, so one autouse
# teardown is what guarantees the early-return and pytest.raises paths
# close theirs too.
_OPENED_DATABASES: list[Database] = []


@pytest_asyncio.fixture(autouse=True)
async def _close_opened_databases():
    yield
    while _OPENED_DATABASES:
        await _OPENED_DATABASES.pop().close()


async def _new_session(tmp_path, *, warm_identity=True, logged_in=True):
    db = Database(str(tmp_path / "104.db"))
    await db.init()
    _OPENED_DATABASES.append(db)
    pool = SessionPool()
    ctx = FakeCtx(pool, db)
    if logged_in:
        # A WARM identity by default: with account.json cold,
        # ensure_account_identity issues an extra event_last_info request
        # through guarded_api before the tool's own sequence — pre-existing
        # behaviour, covered by its own case below.
        label = "operator@example.invalid" if warm_identity else None
        pool.activate_direct(get_session_id(ctx), SessionInfo(cookies=[], account_label=label))
    return ctx, db


def _files_in(ctx) -> list[str]:
    directory = ctx.request_context.lifespan_context.config.resume_files_dir
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir())


# ── exactly two sub-requests, in a fixed order ────────────────────────────

@pytest.mark.asyncio
async def test_a_photo_call_is_exactly_two_requests_detail_then_asset(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    spy = _Spy([_detail(personal_pic=_PHOTO_URL), _asset_resp(body_bytes=_SYNTH_JPEG)])
    _patch_transports(monkeypatch, spy)

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert spy.calls == [("json", "get_resume_detail"), ("asset", "candidate_photo")]
    assert result["photo"]["format"] == "jpeg"


@pytest.mark.asyncio
async def test_an_attachment_call_is_exactly_two_requests_detail_then_asset(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    spy = _Spy([_detail(attachments=[_attachment(1)]), _asset_resp(body_bytes=_SYNTH_PDF)])
    _patch_transports(monkeypatch, spy)

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert spy.calls == [("json", "get_resume_detail"), ("asset", "resume_attachment")]
    assert result["attachment"]["format"] == "pdf"


@pytest.mark.asyncio
async def test_a_cold_account_json_adds_one_leading_event_last_info_request(tmp_path, monkeypatch):
    # Pre-existing behaviour of every tool, not introduced here: with no
    # cached identity, require_login resolves it through its own guarded_api
    # call FIRST. It is outside slots_needed=2, and it is why the other
    # cases warm the identity.
    ctx, _db = await _new_session(tmp_path, warm_identity=False)
    spy = _Spy([
        _json_resp({"data": {}, "metadata": {"userEmail": "operator@example.invalid"}}),
        _detail(personal_pic=_PHOTO_URL),
        _asset_resp(body_bytes=_SYNTH_JPEG),
    ])
    _patch_transports(monkeypatch, spy)

    await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert spy.calls == [
        ("json", "event_last_info"),
        ("json", "get_resume_detail"),
        ("asset", "candidate_photo"),
    ]


# ── the first sub-request failing stops everything ────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("first,expected_fragment", [
    (_json_resp({"code": "00004", "message": "找不到對應資源", "detail": []}, status=404), "找不到資料"),
    (RawResponse(status=302, location="https://bsignin.104.com.tw/login",
                 content_type="text/html", body="", body_bytes=b"", parsed_json=None), "已過期"),
    (RawResponse(status=200, location=None, content_type="text/html",
                 body="正在執行安全驗證 Ray ID: SYNTH1 Performance and Security by Cloudflare",
                 body_bytes=b"", parsed_json=None), "Cloudflare"),
])
async def test_a_failing_detail_request_returns_that_failure_and_issues_no_asset_request(
    tmp_path, monkeypatch, first, expected_fragment,
):
    ctx, _db = await _new_session(tmp_path)
    spy = _Spy([first])  # no second item: it must never be consumed
    _patch_transports(monkeypatch, spy)

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert expected_fragment in result["error"]
    assert "photo" not in result
    assert spy.calls == [("json", "get_resume_detail")]
    assert _files_in(ctx) == []


@pytest.mark.asyncio
async def test_not_logged_in_issues_nothing_and_writes_nothing(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path, logged_in=False)
    _never_called(monkeypatch)

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert result == {"error": "請先呼叫 login()"}
    assert _files_in(ctx) == []


# ── the magic whitelist, landing and refusing ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("body_bytes,expected_format,expected_suffix", [
    (_SYNTH_JPEG, "jpeg", ".jpg"),
    (_SYNTH_PNG, "png", ".png"),
    (_SYNTH_PDF, "pdf", ".pdf"),
])
async def test_each_whitelisted_format_lands_with_the_extension_its_magic_dictates(
    tmp_path, monkeypatch, body_bytes, expected_format, expected_suffix,
):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(1)]), _asset_resp(body_bytes=body_bytes),
    ]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    landed = Path(result["attachment"]["path"])
    assert landed.name == f"attach-{CANDIDATE_ID}-1{expected_suffix}"
    assert landed.read_bytes() == body_bytes
    assert result["attachment"]["format"] == expected_format
    assert result["attachment"]["bytes"] == len(body_bytes)


@pytest.mark.asyncio
async def test_unknown_bytes_are_refused_and_leave_nothing_on_disk(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    junk = bytes(range(8, 40))
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(1)]),
        _asset_resp(body_bytes=junk, content_type="application/octet-stream"),
    ]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert "attachment" not in result
    assert junk[:8].hex() in result["error"]
    # Nothing landed — not even an orphaned .part.
    assert _files_in(ctx) == []


@pytest.mark.asyncio
async def test_the_extension_comes_from_the_magic_not_from_104s_filename(tmp_path, monkeypatch):
    # 104 says .pdf; the bytes are a JPEG. The magic wins.
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(1, filename="SYNTHETIC-CANDIDATE-NAME.pdf")]),
        _asset_resp(body_bytes=_SYNTH_JPEG),
    ]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert Path(result["attachment"]["path"]).name == f"attach-{CANDIDATE_ID}-1.jpg"


@pytest.mark.asyncio
async def test_a_docx_filename_does_not_make_a_pk_body_acceptable(tmp_path, monkeypatch):
    # There is no ZIP entry in the whitelist, because a PK signature has
    # never been seen on the wire. 104's own extension buys nothing.
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(1, filename="SYNTHETIC-CANDIDATE-NAME.docx")]),
        _asset_resp(body_bytes=_SYNTH_ZIP),
    ]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert "attachment" not in result
    assert _SYNTH_ZIP[:8].hex() in result["error"]
    assert _files_in(ctx) == []


@pytest.mark.asyncio
async def test_the_landed_filename_never_contains_104s_own_filename(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    leaky = "SYNTHETIC-CANDIDATE-NAME-DO-NOT-LEAK.pdf"
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(1, filename=leaky)]),
        _asset_resp(body_bytes=_SYNTH_PDF),
    ]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    path = result["attachment"]["path"]
    assert "SYNTHETIC-CANDIDATE-NAME-DO-NOT-LEAK" not in path
    assert Path(path).name == f"attach-{CANDIDATE_ID}-1.pdf"
    assert json.dumps(result, ensure_ascii=False).count("DO-NOT-LEAK") == 0


@pytest.mark.asyncio
async def test_the_not_authenticated_page_is_an_error_not_a_file(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(1)]),
        _asset_resp(body_bytes=_NOT_AUTH_HTML.encode("utf-8"), content_type="text/html"),
    ]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert "attachment" not in result
    assert "這不代表這位候選人沒有這個檔案" in result["error"]
    assert _files_in(ctx) == []


# ── candidate_id / sort validation happens before any request ─────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["", "abc", "12345678901a", "../../etc/passwd",
                                    "..", "1234/5678", "1234\\5678", "12 34"])
async def test_a_non_numeric_candidate_id_is_refused_before_any_request(tmp_path, monkeypatch, bad_id):
    ctx, _db = await _new_session(tmp_path)
    _never_called(monkeypatch)

    result = await _tools()["get_candidate_photo"](bad_id, ctx=ctx)

    assert "error" in result and "photo" not in result
    assert _files_in(ctx) == []


@pytest.mark.asyncio
async def test_path_traversal_in_candidate_id_writes_nothing_anywhere(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _never_called(monkeypatch)
    config = ctx.request_context.lifespan_context.config

    result = await _tools()["get_resume_attachment"]("../../../evil", 1, ctx=ctx)

    assert "error" in result
    assert not config.resume_files_dir.exists()
    # And nothing appeared one level up either.
    assert not (config.data_dir.parent / "evil").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_sort", [0, -1, "1", 1.0, True])
async def test_a_sort_that_is_not_a_positive_int_is_refused_before_any_request(tmp_path, monkeypatch, bad_sort):
    ctx, _db = await _new_session(tmp_path)
    _never_called(monkeypatch)

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, bad_sort, ctx=ctx)

    assert "error" in result and "attachment" not in result


# ── "no photo" is a normal state ──────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("personal_pic", [None, "", "   "])
async def test_an_absent_or_empty_personal_pic_is_photo_null_not_an_error(tmp_path, monkeypatch, personal_pic):
    ctx, _db = await _new_session(tmp_path)
    spy = _Spy([_detail(personal_pic=personal_pic)])
    _patch_transports(monkeypatch, spy)

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert result["photo"] is None
    assert "error" not in result
    assert any("沒有放大頭照" in w for w in result["warnings"])
    assert spy.calls == [("json", "get_resume_detail")]
    assert _files_in(ctx) == []


@pytest.mark.asyncio
async def test_104s_own_placeholder_head_shot_is_photo_null_and_never_fetched(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    spy = _Spy([_detail(personal_pic=_PLACEHOLDER_URL)])
    _patch_transports(monkeypatch, spy)

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert result["photo"] is None
    assert any("預設頭像" in w for w in result["warnings"])
    assert spy.calls == [("json", "get_resume_detail")]
    assert _files_in(ctx) == []


@pytest.mark.asyncio
async def test_an_unmeasured_photo_host_is_an_error_naming_only_the_hostname(tmp_path, monkeypatch):
    # NOT photo: null — "we do not fetch from an unmeasured host" and "this
    # candidate has no photo" are different facts.
    ctx, _db = await _new_session(tmp_path)
    url = "https://images.example.invalid/some/path/SYNTHETIC-SECRET?v=ALSO-SECRET"
    spy = _Spy([_detail(personal_pic=url)])
    _patch_transports(monkeypatch, spy)

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert "photo" not in result
    assert "images.example.invalid" in result["error"]
    for forbidden in ("SYNTHETIC-SECRET", "ALSO-SECRET", "/some/path", "?v="):
        assert forbidden not in result["error"]
    assert spy.calls == [("json", "get_resume_detail")]


# ── attachment selection ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_missing_sort_lists_the_available_ones(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([_detail(attachments=[
        _attachment(1, title="SYNTHETIC-TITLE-ONE"),
        _attachment(2, title="SYNTHETIC-TITLE-TWO"),
    ])]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 3, ctx=ctx)

    assert "attachment" not in result
    assert result["available"] == [
        {"sort": 1, "title": "SYNTHETIC-TITLE-ONE"},
        {"sort": 2, "title": "SYNTHETIC-TITLE-TWO"},
    ]
    assert "不是抓取失敗" in result["error"]


@pytest.mark.asyncio
async def test_a_resume_with_no_attachments_at_all_omits_available(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([_detail(attachments=[])]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert "available" not in result
    assert "沒有附件" in result["error"]
    assert "不是抓取失敗" in result["error"]


@pytest.mark.asyncio
async def test_the_returned_attachment_omits_filename_link_and_preview(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(
            1, filename="SYNTHETIC-CANDIDATE-NAME.pdf",
            link=_ATTACH_URL,
            preview="https://asset.vip.104.com.tw/download/webImg/SYNTHETIC-PREVIEW",
        )]),
        _asset_resp(body_bytes=_SYNTH_PDF),
    ]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    attachment = result["attachment"]
    assert set(attachment) == {"path", "bytes", "format", "sort", "title", "type"}
    text = json.dumps(result, ensure_ascii=False)
    for forbidden in ("filename", "link", "preview", "SYNTHETIC-TOKEN-BBB",
                      "SYNTHETIC-PREVIEW", "SYNTHETIC-CANDIDATE-NAME"):
        assert forbidden not in text
    # title/type pass through verbatim; `type`'s value domain is unmeasured
    # and is not interpreted.
    assert attachment["title"] == "SYNTHETIC-TITLE"
    assert attachment["type"] == 1


# ── the return shape is fixed ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_successful_photo_call_has_a_fixed_key_set_with_warnings_always_present(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(personal_pic=_PHOTO_URL), _asset_resp(body_bytes=_SYNTH_JPEG),
    ]))

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert set(result) == {"photo", "browse_limit", "warnings"}
    assert set(result["photo"]) == {"path", "bytes", "format"}
    assert isinstance(result["warnings"], list)


# ── browse_limit comes from the first sub-request ─────────────────────────

@pytest.mark.asyncio
async def test_browse_limit_is_reported_from_the_detail_response(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(personal_pic=_PHOTO_URL, browse_limit={"resumeMax": 300, "onThatDayCount": "27"}),
        _asset_resp(body_bytes=_SYNTH_JPEG),
    ]))

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert result["browse_limit"] == {"resume_max": 300, "on_that_day_count": 27}
    # A number was delivered, so the sentence explaining it belongs.
    assert any("browse_limit" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_an_absent_browse_limit_key_is_null_not_a_structural_failure(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(personal_pic=_PHOTO_URL), _asset_resp(body_bytes=_SYNTH_JPEG),
    ]))

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert result["browse_limit"] is None
    assert result["photo"] is not None
    # ...and the note that explains what browse_limit means is not there to
    # gloss a number the caller was never given. warnings still exists.
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_the_ninety_percent_threshold_adds_a_warning(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(personal_pic=_PHOTO_URL, browse_limit={"resumeMax": 300, "onThatDayCount": "270"}),
        _asset_resp(body_bytes=_SYNTH_JPEG),
    ]))

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    assert any("270/300" in w for w in result["warnings"])


# ── nothing else from the résumé escapes ──────────────────────────────────

@pytest.mark.asyncio
async def test_no_other_resume_field_reaches_the_return_value_warnings_log_or_filename(
    tmp_path, monkeypatch, caplog,
):
    ctx, _db = await _new_session(tmp_path)
    secrets = {
        "userName": "SYNTHETIC-PERSON-NAME",
        "phone": "SYNTHETIC-PHONE-0000",
        "email": "synthetic@example.invalid",
        "autobiography": "SYNTHETIC-AUTOBIOGRAPHY-TEXT",
    }
    _patch_transports(monkeypatch, _Spy([
        _detail(personal_pic=_PHOTO_URL, extra_resume=secrets),
        _asset_resp(body_bytes=_SYNTH_JPEG),
    ]))

    with caplog.at_level(logging.DEBUG):
        result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    text = json.dumps(result, ensure_ascii=False)
    for value in secrets.values():
        assert value not in text
        assert value not in caplog.text
        assert value not in result["photo"]["path"]
    assert all(value not in name for value in secrets.values() for name in _files_in(ctx))


# ── this round writes nothing to the database ─────────────────────────────

@pytest.mark.asyncio
async def test_neither_tool_writes_a_sent_log_or_a_candidate_row(tmp_path, monkeypatch):
    ctx, db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(personal_pic=_PHOTO_URL, attachments=[_attachment(1)]),
        _asset_resp(body_bytes=_SYNTH_JPEG),
        _detail(personal_pic=_PHOTO_URL, attachments=[_attachment(1)]),
        _asset_resp(body_bytes=_SYNTH_PDF),
    ]))

    await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)
    await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    for table in ("candidates", "sent_log"):
        cursor = await db._conn.execute(f"SELECT COUNT(*) FROM {table}")
        assert (await cursor.fetchone())[0] == 0


# ── the timeout wording is differentiated by WHICH sub-request failed ─────

@pytest.mark.asyncio
async def test_an_asset_timeout_says_do_not_retry_repeatedly(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(1)]), TimeoutError("synthetic timeout"),
    ]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert "請不要連續重試" in result["error"]
    assert "6.7 MB" in result["error"]
    assert _files_in(ctx) == []


@pytest.mark.asyncio
async def test_a_detail_timeout_keeps_the_ordinary_transport_wording(tmp_path, monkeypatch):
    # The half that proves the differentiation did not overreach: only the
    # SECOND sub-request gets the large-file wording.
    ctx, _db = await _new_session(tmp_path)
    _patch_transports(monkeypatch, _Spy([TimeoutError("synthetic timeout")]))

    result = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert result["error"] == "104 API 請求失敗（可能是逾時或網路問題），請稍後再試"
    assert "請不要連續重試" not in result["error"]


# ── repeat fetches overwrite ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetching_the_same_asset_twice_overwrites_rather_than_accumulating(tmp_path, monkeypatch):
    ctx, _db = await _new_session(tmp_path)
    second_bytes = _SYNTH_PDF + b"SECOND"
    _patch_transports(monkeypatch, _Spy([
        _detail(attachments=[_attachment(1)]), _asset_resp(body_bytes=_SYNTH_PDF),
        _detail(attachments=[_attachment(1)]), _asset_resp(body_bytes=second_bytes),
    ]))

    first = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)
    second = await _tools()["get_resume_attachment"](CANDIDATE_ID, 1, ctx=ctx)

    assert first["attachment"]["path"] == second["attachment"]["path"]
    assert _files_in(ctx) == [f"attach-{CANDIDATE_ID}-1.pdf"]
    assert Path(second["attachment"]["path"]).read_bytes() == second_bytes


# ── the retention sweep ───────────────────────────────────────────────────

def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_the_sweep_deletes_expired_files_keeps_fresh_ones_and_removes_orphan_parts(tmp_path):
    directory = tmp_path / "resume-files"
    directory.mkdir()
    stale = directory / "attach-1111111111111-1.pdf"
    fresh = directory / "attach-2222222222222-1.pdf"
    orphan = directory / "attach-3333333333333-1.pdf.999-abcdef12.part"
    for path in (stale, fresh, orphan):
        path.write_bytes(_SYNTH_PDF)
    _age(stale, RESUME_FILE_RETENTION_SECONDS + 60)
    _age(orphan, RESUME_FILE_RETENTION_SECONDS + 60)

    sweep_expired_files(directory)

    assert sorted(p.name for p in directory.iterdir()) == [fresh.name]


@pytest.mark.asyncio
async def test_a_successful_fetch_sweeps_before_it_writes(tmp_path, monkeypatch):
    """The sweep is well covered on its own; its PLACEMENT in the write path
    is what this pins. An expired file must be gone by the time the new one
    lands, and the sweep must run while the new file does not yet exist —
    a sweep after the write would work today only because the new file is
    fresh, which is a property of the clock, not of the ordering.
    """
    ctx, _db = await _new_session(tmp_path)
    directory = ctx.request_context.lifespan_context.config.resume_files_dir
    directory.mkdir(parents=True, exist_ok=True)
    stale = directory / "attach-9999999999999-1.pdf"
    stale.write_bytes(_SYNTH_PDF)
    _age(stale, RESUME_FILE_RETENTION_SECONDS + 60)

    seen_at_sweep_time: list[list[str]] = []
    real_sweep = resume_files_mod.sweep_expired_files

    def recording_sweep(path):
        seen_at_sweep_time.append(_files_in(ctx))
        return real_sweep(path)

    monkeypatch.setattr(resume_files_mod, "sweep_expired_files", recording_sweep)
    _patch_transports(monkeypatch, _Spy([
        _detail(personal_pic=_PHOTO_URL), _asset_resp(body_bytes=_SYNTH_JPEG),
    ]))

    result = await _tools()["get_candidate_photo"](CANDIDATE_ID, ctx=ctx)

    landed = Path(result["photo"]["path"])
    assert seen_at_sweep_time == [[stale.name]], "swept exactly once, before the write"
    assert landed.name not in seen_at_sweep_time[0]
    assert _files_in(ctx) == [landed.name]


def test_the_sweep_on_a_directory_that_does_not_exist_is_not_an_error(tmp_path):
    # Lazy creation makes "absent" the normal first-run state.
    sweep_expired_files(tmp_path / "never-created")


class _UnreadableDir:
    """A directory whose listing fails. A stand-in object rather than a
    monkeypatch of Path.iterdir, which pytest's own machinery also calls."""

    def iterdir(self):
        raise PermissionError("synthetic permission failure")

    def __str__(self):
        return "<unreadable resume-files dir>"


def test_a_read_failure_is_logged_and_swallowed_not_raised(caplog):
    # Deliberately unlike compact_state_file, which re-raises: this
    # directory is a precondition of nothing, and blocking startup over it
    # would cost the user even the ability to call login().
    with caplog.at_level(logging.WARNING):
        sweep_expired_files(_UnreadableDir())
    assert "synthetic permission failure" in caplog.text


def test_startup_calls_the_same_sweep_and_a_read_failure_does_not_abort_it():
    # main.py calls this very function during startup, so the swallow above
    # is what keeps a permissions problem from blocking the server.
    import mcp104.main as main_mod

    assert main_mod.sweep_expired_files is sweep_expired_files
    main_mod.sweep_expired_files(_UnreadableDir())


# ── atomic write ──────────────────────────────────────────────────────────

def test_a_failed_replace_leaves_no_target_and_no_orphan_part(tmp_path, monkeypatch):
    directory = tmp_path / "resume-files"

    def failing_replace(src, dst):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(resume_files_mod.os, "replace", failing_replace)
    with pytest.raises(OSError):
        write_asset_atomically(directory, "attach-1-1.pdf", _SYNTH_PDF)

    assert not (directory / "attach-1-1.pdf").exists()
    assert list(directory.iterdir()) == []


def test_a_failed_replace_over_an_existing_file_leaves_the_old_one_intact(tmp_path, monkeypatch):
    # The case that actually carries weight: os.replace only promises no
    # half-swap is observed, so the pre-existing file must survive unchanged.
    directory = tmp_path / "resume-files"
    directory.mkdir()
    target = directory / "attach-1-1.pdf"
    target.write_bytes(_SYNTH_PDF)

    def failing_replace(src, dst):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(resume_files_mod.os, "replace", failing_replace)
    with pytest.raises(OSError):
        write_asset_atomically(directory, "attach-1-1.pdf", _SYNTH_JPEG)

    assert target.read_bytes() == _SYNTH_PDF
    assert sorted(p.name for p in directory.iterdir()) == ["attach-1-1.pdf"]


def test_the_part_filename_is_unique_per_write(tmp_path, monkeypatch):
    # A temp name derived only from the target would let two concurrent
    # fetches of the same asset interleave into one file and then publish a
    # corrupt result atomically.
    directory = tmp_path / "resume-files"
    seen: list[str] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(resume_files_mod.os, "replace", recording_replace)
    write_asset_atomically(directory, "attach-1-1.pdf", _SYNTH_PDF)
    write_asset_atomically(directory, "attach-1-1.pdf", _SYNTH_PDF)

    assert len(set(seen)) == 2
    for name in seen:
        assert name.startswith("attach-1-1.pdf.")
        assert name.endswith(".part")
        assert str(os.getpid()) in name


def test_the_directory_is_created_lazily_at_first_write(tmp_path):
    directory = tmp_path / "resume-files"
    assert not directory.exists()
    write_asset_atomically(directory, "attach-1-1.pdf", _SYNTH_PDF)
    assert (directory / "attach-1-1.pdf").exists()


# ── module hygiene ────────────────────────────────────────────────────────

def test_the_module_does_not_use_future_annotations():
    # A module that registers tools must not: mcp's Tool.from_function uses
    # a real issubclass() check to strip `ctx` from the published schema,
    # and PEP 563 turns every annotation into a string, which silently
    # fails that check and leaks `ctx` into inputSchema.required.
    source = Path(resume_files_mod.__file__).read_text(encoding="utf-8")
    assert "from __future__ import annotations" not in source
