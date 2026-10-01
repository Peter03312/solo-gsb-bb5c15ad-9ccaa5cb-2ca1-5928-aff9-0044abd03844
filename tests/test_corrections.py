"""人群组纠正清单（批量纠正未出发/失败组字段）集成测试。

覆盖：
* 请求级校验：清单非空、每项至少一个字段、字段值合法、同批组编号不重复；
* 状态校验：组不存在、最终字段不满足组校验（截止早于最早出发）、
  起点非场馆节点（均 422 整批拒绝）；
* 目标组已出发（含并发时间推进所致）整批拒绝（409），状态与修订历史不变；
* 成功时整批一次生效、按现有规则整体重排未出发组、已出发组路线固定；
* 修订记录：corrections 操作、每项纠正前后字段、编排差异；拒绝不产生修订；
* 并发纠正串行化：修订号连续无重复。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 与其他测试文件同进程运行时 app.main 只加载一次（以先加载者的环境变量
# 为准）；此处设置保证本文件可独立运行。断言均不依赖修订号绝对取值。
_tmpdir = tempfile.mkdtemp(prefix="evac-test-corrections-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
        {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
        {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
        {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
        {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20},
    ],
    "exits": [5, 6],
    "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
    "groups": [
        {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 20, "deadline": 20},
        {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0,
         "priority": 5, "deadline": 15},
    ],
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=SCENARIO)
        assert resp.status_code == 200
        yield c


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def revs(c):
    return c.get("/revisions").json()["revisions"]


def latest_rev(c):
    return revs(c)[-1]["rev"]


# --------------------------------------------------------------------------
# 成功：整批生效与整体重排
# --------------------------------------------------------------------------

def test_correction_applies_final_values_and_replans(client):
    # 组 3 起点 2 -> 7：改经通道 7 直达出口 5；组 4 与其共用通道 7
    # （容量 20，组 3 占 10 人），整体重排后组 4 等组 3 走完后出发
    resp = client.post("/corrections", json=[{"group_id": 3, "origin": 7}])
    assert resp.status_code == 200
    groups = by_id(resp.json())
    assert groups[3]["status"] == "scheduled"
    assert groups[3]["route"] == [7]
    assert groups[3]["exit_id"] == 5
    assert groups[3]["departure_minute"] == 0
    assert groups[3]["arrival_minute"] == 5
    assert groups[4]["route"] == [7]
    assert groups[4]["departure_minute"] == 5
    assert groups[4]["arrival_minute"] == 10
    # 组 1 不受影响
    assert groups[1]["route"] == [1, 5]
    assert groups[1]["departure_minute"] == 10
    assert groups[1]["arrival_minute"] == 16


def test_batch_corrections_apply_atomically_as_one_revision(client):
    n0 = len(revs(client))
    resp = client.post("/corrections", json=[
        {"group_id": 1, "priority": 30},
        {"group_id": 4, "size": 12, "deadline": 8}])
    assert resp.status_code == 200
    # 整批一条修订，detail 记录每项纠正前后字段
    assert len(revs(client)) == n0 + 1
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    assert detail["operation"] == "corrections"
    corrs = {c["group_id"]: c for c in detail["detail"]["corrections"]}
    assert corrs[1]["fields"] == ["priority"]
    assert corrs[1]["before"]["priority"] == 10
    assert corrs[1]["after"]["priority"] == 30
    assert corrs[4]["fields"] == ["size", "deadline"]
    assert corrs[4]["before"]["size"] == 15
    assert corrs[4]["after"]["size"] == 12
    assert corrs[4]["after"]["deadline"] == 8
    # 未指定字段沿用原值
    assert corrs[4]["after"]["origin"] == 7
    assert corrs[4]["after"]["priority"] == 5


def test_failed_group_can_be_corrected(client):
    # 组 9 人数 200 超任意通道容量 -> 编排失败；纠正人数后可安排
    resp = client.post("/groups", json=[
        {"id": 9, "size": 200, "origin": 2, "earliest_departure": 0,
         "priority": 1, "deadline": 20}])
    assert resp.status_code == 201
    assert by_id(resp.json())[9]["status"] == "failed"

    resp = client.post("/corrections", json=[{"group_id": 9, "size": 10}])
    assert resp.status_code == 200
    g9 = by_id(resp.json())[9]
    assert g9["status"] == "scheduled"
    assert g9["route"] == [5]
    assert g9["departure_minute"] == 0
    assert g9["arrival_minute"] == 2


def test_correction_replan_keeps_departed_routes_fixed(client):
    # 推进 1 分钟：组 3、组 4（均 t=0 出发）固定；纠正组 1 不影响其路线
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    resp = client.post("/corrections", json=[{"group_id": 1, "priority": 30}])
    assert resp.status_code == 200
    groups = by_id(resp.json())
    assert groups[3]["status"] == "fixed"
    assert groups[3]["route"] == [5]
    assert groups[3]["departure_minute"] == 0
    assert groups[3]["arrival_minute"] == 2
    assert groups[4]["status"] == "fixed"
    assert groups[4]["route"] == [7]
    # 已出发组占用保留：通道 5 第 1 分钟仍被组 3 占用 10 人
    load = client.get("/passages/5/load?minute=1").json()
    assert load["minutes"][0]["used"] == 10
    assert load["minutes"][0]["groups"] == [
        {"group_id": 3, "size": 10, "status": "fixed"}]


# --------------------------------------------------------------------------
# 整批拒绝：请求无效（422）
# --------------------------------------------------------------------------

def test_empty_correction_list_rejected(client):
    assert client.post("/corrections", json=[]).status_code == 422


def test_item_must_specify_at_least_one_field(client):
    assert client.post("/corrections",
                       json=[{"group_id": 3}]).status_code == 422


def test_field_values_validated(client):
    assert client.post("/corrections", json=[
        {"group_id": 3, "size": 0}]).status_code == 422
    assert client.post("/corrections", json=[
        {"group_id": 3, "priority": -1}]).status_code == 422
    assert client.post("/corrections", json=[
        {"group_id": 3, "deadline": -1}]).status_code == 422
    assert client.post("/corrections", json=[
        {"group_id": 3, "earliest_departure": -1}]).status_code == 422


def test_duplicate_group_id_in_batch_rejected(client):
    plan_before = client.get("/plan").json()
    resp = client.post("/corrections", json=[
        {"group_id": 3, "size": 12}, {"group_id": 3, "priority": 30}])
    assert resp.status_code == 422
    assert client.get("/plan").json() == plan_before


def test_unknown_group_rejected(client):
    assert client.post("/corrections", json=[
        {"group_id": 999, "size": 5}]).status_code == 422


def test_merged_fields_must_satisfy_group_validation(client):
    # 组 3 截止 20：仅把最早出发改为 25 -> 最终字段不满足组校验
    resp = client.post("/corrections", json=[
        {"group_id": 3, "earliest_departure": 25}])
    assert resp.status_code == 422
    # 同项内给出两者但自相矛盾同样拒绝
    resp = client.post("/corrections", json=[
        {"group_id": 3, "earliest_departure": 30, "deadline": 25}])
    assert resp.status_code == 422


def test_origin_must_be_venue_node(client):
    assert client.post("/corrections", json=[
        {"group_id": 3, "origin": 999}]).status_code == 422


def test_batch_with_any_invalid_item_fully_rejected(client):
    n0 = len(revs(client))
    plan_before = client.get("/plan").json()
    resp = client.post("/corrections", json=[
        {"group_id": 1, "priority": 30},   # 本身合法
        {"group_id": 999, "size": 5}])     # 组不存在 -> 整批拒绝
    assert resp.status_code == 422
    assert client.get("/plan").json() == plan_before
    assert len(revs(client)) == n0
    # 组 1 未被修改：随后单独纠正时修订记录的原值仍为 10
    assert client.post("/corrections", json=[
        {"group_id": 1, "size": 28}]).status_code == 200
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    corr = detail["detail"]["corrections"][0]
    assert corr["before"]["priority"] == 10
    assert corr["after"]["size"] == 28


# --------------------------------------------------------------------------
# 整批拒绝：目标组已出发（409）
# --------------------------------------------------------------------------

def test_departed_group_correction_rejected(client):
    # 推进 1 分钟：组 3（t=0 出发）固定，不可再纠正
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    n0 = len(revs(client))
    plan_before = client.get("/plan").json()

    resp = client.post("/corrections", json=[{"group_id": 3, "size": 8}])
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "GROUP_ALREADY_DEPARTED"
    assert body["conflicts"] == [{"group_id": 3, "status": "fixed"}]
    # 整批拒绝：状态与修订历史不变
    assert client.get("/plan").json() == plan_before
    assert len(revs(client)) == n0
    # 未出发的组 1 仍可纠正
    assert client.post("/corrections", json=[
        {"group_id": 1, "deadline": 28}]).status_code == 200


def test_batch_with_departed_group_fully_rejected(client):
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    n0 = len(revs(client))
    # 组 4 t=0 已出发：混入整批后整批拒绝，组 1 的合法项也不生效
    resp = client.post("/corrections", json=[
        {"group_id": 1, "priority": 30},
        {"group_id": 4, "size": 10}])
    assert resp.status_code == 409
    assert resp.json()["conflicts"] == [{"group_id": 4, "status": "fixed"}]
    assert len(revs(client)) == n0
    # 组 1 未受影响（原优先级仍为 10）
    assert client.post("/corrections", json=[
        {"group_id": 1, "size": 28}]).status_code == 200
    detail = client.get(f"/revisions/{latest_rev(client)}").json()
    assert detail["detail"]["corrections"][0]["before"]["priority"] == 10


def test_correction_racing_tick_is_atomic(client):
    """纠正与时间推进并发：要么纠正先生效（随后可能被推进固定），
    要么推进使目标组已出发而整批 409；不会出现部分生效。"""
    from concurrent.futures import ThreadPoolExecutor

    n0 = len(revs(client))
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_tick = pool.submit(client.post, "/tick", json={"minutes": 1})
        f_corr = pool.submit(client.post, "/corrections",
                             json=[{"group_id": 3, "priority": 30}])
        r_tick, r_corr = f_tick.result(), f_corr.result()
    assert r_tick.status_code == 200
    assert r_corr.status_code in (200, 409)
    entries = revs(client)
    if r_corr.status_code == 409:
        assert r_corr.json()["error"] == "GROUP_ALREADY_DEPARTED"
        assert len(entries) == n0 + 1   # 仅 tick 产生修订
    else:
        assert len(entries) == n0 + 2
    # 无论先后顺序，组 3 处于一致状态
    g3 = by_id(client.get("/plan").json())[3]
    assert g3["status"] in ("scheduled", "fixed")


# --------------------------------------------------------------------------
# 修订历史
# --------------------------------------------------------------------------

def test_correction_revision_records_fields_and_plan_diff(client):
    n0 = len(revs(client))
    resp = client.post("/corrections", json=[
        {"group_id": 3, "origin": 7, "priority": 25}])
    assert resp.status_code == 200
    entries = revs(client)
    assert len(entries) == n0 + 1
    entry = entries[-1]
    assert entry["operation"] == "corrections"
    assert entry["minute"] == 0

    detail = client.get(f"/revisions/{entry['rev']}").json()
    corr = detail["detail"]["corrections"][0]
    assert corr["group_id"] == 3
    assert corr["fields"] == ["origin", "priority"]
    assert corr["before"] == {"size": 10, "origin": 2,
                              "earliest_departure": 0, "priority": 20,
                              "deadline": 20}
    assert corr["after"] == {"size": 10, "origin": 7,
                             "earliest_departure": 0, "priority": 25,
                             "deadline": 20}
    # 编排差异：组 3 改道、组 4 因共用通道 7 推迟出发
    changes = {ch["group_id"]: ch for ch in detail["changes"]}
    assert changes[3]["change"] == "changed"
    assert changes[3]["before"]["route"] == [5]
    assert changes[3]["after"]["route"] == [7]
    assert changes[4]["before"]["departure_minute"] == 0
    assert changes[4]["after"]["departure_minute"] == 5
    # 快照为提交后的完整编排
    snap = by_id(detail["snapshot"])
    assert snap[3]["route"] == [7]


def test_rejected_corrections_create_no_revision(client):
    n0 = len(revs(client))
    client.post("/corrections", json=[])
    client.post("/corrections", json=[{"group_id": 3}])
    client.post("/corrections", json=[{"group_id": 999, "size": 5}])
    client.post("/corrections", json=[
        {"group_id": 3, "size": 1}, {"group_id": 3, "size": 2}])
    client.post("/corrections", json=[{"group_id": 3, "origin": 999}])
    assert len(revs(client)) == n0


def test_correction_revision_survives_scenario_reset(client):
    client.post("/corrections", json=[{"group_id": 3, "size": 12}])
    rev = latest_rev(client)
    detail_before = client.get(f"/revisions/{rev}").json()
    assert client.post("/scenario", json=SCENARIO).status_code == 200
    assert client.get(f"/revisions/{rev}").json() == detail_before


# --------------------------------------------------------------------------
# 并发编号
# --------------------------------------------------------------------------

def test_concurrent_corrections_serialized(client):
    """并发纠正不同组：全部成功，各产生一条修订，编号连续无重复。"""
    from concurrent.futures import ThreadPoolExecutor

    payloads = [
        [{"group_id": 1, "priority": 11}],
        [{"group_id": 3, "priority": 21}],
        [{"group_id": 4, "priority": 6}],
    ]
    n0 = len(revs(client))
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(
            lambda p: client.post("/corrections", json=p), payloads))
    assert all(r.status_code == 200 for r in responses)
    entries = revs(client)
    assert len(entries) == n0 + 3
    new = entries[n0:]
    assert [e["operation"] for e in new] == ["corrections"] * 3
    new_revs = [e["rev"] for e in new]
    assert new_revs == list(range(new_revs[0], new_revs[0] + 3))
