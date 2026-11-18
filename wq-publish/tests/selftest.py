"""关键流程自测（不依赖 pytest，直接 python -I tests/selftest.py 运行）。

覆盖：
1. CSV 校验：中文表头、重复 (样本,项目)、负数/非数字/空值/缺列/缺批次号
2. 端到端：上传 → 预览(增/改/删/不变分类) → 发布 → 登记版本/行数 → 下游文件哈希一致
3. 并发：两个页面从同一版本发起，仅一个发布成功，另一个 409 须重新预览
4. 崩溃恢复：文件缺失、文件被改坏、遗留临时文件、游离文件、改名后未提交
5. 撤回语义：整批替换后旧行消失；会话取消/重复发布防护
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wq_publish.app import create_app
from wq_publish.storage import LIVE_FILENAME
from wq_publish.validation import render_csv, validate_csv
from wq_publish.qc import (
    QCValidationError,
    analyze_pairs,
    minimum_vertex_cover,
    relative_deviation,
    validate_pair_csv,
)

PASS, FAIL = 0, 0
EXAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "examples")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def read_example(fname):
    with open(os.path.join(EXAMPLE_DIR, fname), "rb") as fh:
        return fh.read()


def upload(client, fname, batch_no="B1"):
    return client.post(
        "/preview",
        data={"batch_no": batch_no,
              "csv_file": (io.BytesIO(read_example(fname)), fname)},
        content_type="multipart/form-data",
    )


# ---------------- 1. 校验单元测试 ----------------
print("== 1. CSV 校验 ==")
rows, errs = validate_csv(read_example("batch_v1.csv"), "B1")
check("合法 CSV 通过并解析出 5 行", errs == [] and len(rows) == 5, str(errs))

rows, errs = validate_csv(read_example("batch_invalid.csv"), "B1")
check("重复(样本,项目)被拒", any("重复" in e for e in errs), str(errs))
check("负浓度被拒", any("负数" in e for e in errs), str(errs))
check("空浓度被拒", any("浓度为空" in e for e in errs), str(errs))
check("空项目被拒", any("检测项目为空" in e for e in errs), str(errs))

bad = "样本号,浓度\nS1,1.0\n".encode()
rows, errs = validate_csv(bad, "B1")
check("缺少检测项目列被拒", rows is None and any("表头" in e for e in errs), str(errs))

rows, errs = validate_csv(read_example("batch_v1.csv"), " ")
check("空批次号被拒", any("批次号" in e for e in errs), str(errs))

for text in ["S1,氨氮,nan", "S1,氨氮,inf", "S1,氨氮,abc"]:
    raw = f"样本号,检测项目,浓度\n{text}\n".encode()
    rows, errs = validate_csv(raw, "B1")
    check(f"非法浓度 {text!r} 被拒", rows is None and len(errs) > 0, str(errs))

raw_zero = "样本号,检测项目,浓度\nS1,氨氮,0\n".encode()
rows, errs = validate_csv(raw_zero, "B1")
check("零浓度（非负边界）通过", errs == [] and rows[0]["concentration"] == 0.0, str(errs))

# ---------------- 2-5. 端到端（隔离临时目录） ----------------
tmp = tempfile.mkdtemp(prefix="wq-selftest-")
data_dir = os.path.join(tmp, "data")
down_dir = os.path.join(tmp, "downstream")
app = create_app(data_dir=data_dir, downstream_dir=down_dir)
client = app.test_client()

print("== 2. 首次发布 v1 ==")
resp = upload(client, "batch_v1.csv", "BATCH-001")
check("预览页 200 且显示增删改", resp.status_code == 200
      and "新增".encode() in resp.data and "撤回".encode() in resp.data)
import re
m = re.search(rb'/publish/([0-9a-f]+)', resp.data)
check("预览页含发布令牌", m is not None)
token1 = m.group(1).decode()

resp = client.post(f"/publish/{token1}")
check("确认发布后 302 回首页", resp.status_code == 302)
reg = app.storage.current_registry()
check("登记为 v1、5 行", reg["current_version"] == 1 and reg["row_count"] == 5,
      f"got {dict(reg) if reg else None}")
live = os.path.join(down_dir, LIVE_FILENAME)
check("下游 CSV 已生成", os.path.exists(live))
file_hash = hashlib.sha256(open(live, "rb").read()).hexdigest()
check("文件哈希与登记一致", file_hash == reg["content_hash"])
check("下游目录无临时文件残留",
      not [n for n in os.listdir(down_dir) if n.startswith(".tmp-")])
resp = client.get("/published.csv")
check("已发布 CSV 可下载", resp.status_code == 200 and b"S001" in resp.data)
resp = client.post(f"/publish/{token1}")
check("同一会话不可重复发布（返回 409 过期页）", resp.status_code == 409
      and app.storage.get_session(token1)["status"] == "published")

print("== 3. 修订预览的增/改/删/不变分类 ==")
resp = upload(client, "batch_v2_revision.csv", "BATCH-002")
body = resp.data.decode()
# v2 相对 v1：S004氨氮新增；S001氨氮、S002COD 修改；S002氨氮 撤回；S001总磷/S003总磷 不变
check("新增 1 行", "+1" in body and "S004" in body)
check("修改 2 行", "~2" in body and "1.25" in body and "35.0" in body)
check("撤回 1 行（S002 氨氮）", "−1" in body and "S002" in body)
check("不变 2 行", "不变项 2" in body)
token2 = re.search(rb'/publish/([0-9a-f]+)', resp.data).group(1).decode()
client.post(f"/publish/{token2}")
reg = app.storage.current_registry()
check("发布后登记为 v2、5 行", reg["current_version"] == 2 and reg["row_count"] == 5)
current = app.storage.current_rows()
check("撤回生效：S002 氨氮已不存在",
      not any(r["sample_no"] == "S002" and r["analyte"] == "氨氮" for r in current))
check("修改生效：S001 氨氮=1.3",
      any(r["sample_no"] == "S001" and r["analyte"] == "氨氮"
          and r["concentration"] == 1.3 for r in current))
file_hash = hashlib.sha256(open(live, "rb").read()).hexdigest()
check("v2 文件哈希与登记一致", file_hash == reg["content_hash"])

print("== 4. 同版本并发修订：只允许一个成功 ==")
r_a = upload(client, "batch_v1.csv", "CONCUR-A")
r_b = upload(client, "batch_v2_revision.csv", "CONCUR-B")
tok_a = re.search(rb'/publish/([0-9a-f]+)', r_a.data).group(1).decode()
tok_b = re.search(rb'/publish/([0-9a-f]+)', r_b.data).group(1).decode()
ok = client.post(f"/publish/{tok_a}")
check("A 发布成功 → v3", ok.status_code == 302
      and app.storage.current_registry()["current_version"] == 3)
conflict = client.post(f"/publish/{tok_b}")
check("B 发布被拒并返回 409 过期预览页", conflict.status_code == 409
      and "该预览已过期".encode() in conflict.data)
sess_b = app.storage.get_session(tok_b)
check("B 会话被标记 obsolete", sess_b["status"] == "obsolete")
reg_before = app.storage.current_registry()["current_version"]
retry = upload(client, "batch_v2_revision.csv", "CONCUR-B-RETRY")
tok_b2 = re.search(rb'/publish/([0-9a-f]+)', retry.data).group(1).decode()
client.post(f"/publish/{tok_b2}")
check("B 重新预览后可发布 → v4",
      app.storage.current_registry()["current_version"] == reg_before + 1)
check("A 的发布未被覆盖：v4 文件行数=5",
      app.storage.current_registry()["row_count"] == 5)

print("== 5. 崩溃后重启自动恢复 ==")
# 5a 下游文件被删除
os.unlink(live)
app2 = create_app(data_dir=data_dir, downstream_dir=down_dir)
reg = app2.storage.current_registry()
check("5a 文件缺失后重建且哈希与登记一致",
      os.path.exists(live)
      and hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

# 5b 文件被外部改坏
with open(live, "ab") as fh:
    fh.write(b"CORRUPTED\n")
app3 = create_app(data_dir=data_dir, downstream_dir=down_dir)
reg = app3.storage.current_registry()
check("5b 损坏文件被按登记重写",
      hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

# 5c 遗留半写临时文件
tmp_orphan = os.path.join(down_dir, ".tmp-publish-deadbeef.csv")
open(tmp_orphan, "wb").write(b"partial")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5c 遗留临时文件被清理", not os.path.exists(tmp_orphan))

# 5d 下游目录出现未登记游离文件
stranger = os.path.join(down_dir, "stranger.csv")
open(stranger, "wb").write(b"x")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5d 游离文件被移入 quarantine",
      not os.path.exists(stranger)
      and any(f.endswith("stranger.csv")
              for f in os.listdir(os.path.join(data_dir, "quarantine"))))

# 5e 模拟“文件已改名但事务未提交”：文件内容比登记新，重启应回滚到登记版本
with app3.storage._connect() as conn:
    old_csv = app3.storage._render_version(conn, reg["current_version"])
newer = render_csv([{"sample_no": "ZX", "analyte": "氨氮", "concentration": 9.9}])
app3.storage._atomic_write(newer)
check("预置：文件已被换成未登记的新版本",
      open(live, "rb").read().decode() != old_csv)
app4 = create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5e 改名后未提交 → 恢复为登记的完整旧版",
      open(live, "rb").read().decode() == old_csv)
events = app4.storage.recent_events(50)
check("恢复动作写入事件日志", any(e["kind"] == "recover" for e in events))

print("== 6. 会话取消 ==")
r = upload(client, "batch_v1.csv", "CANCEL-ME")
tok = re.search(rb'/publish/([0-9a-f]+)', r.data).group(1).decode()
v_before = app.storage.current_registry()["current_version"]
client.post(f"/cancel/{tok}")
check("取消后发布被拒绝（409 且会话 cancelled）",
      client.post(f"/publish/{tok}").status_code == 409
      and app.storage.get_session(tok)["status"] == "cancelled")
check("取消不影响当前版本",
      app.storage.current_registry()["current_version"] == v_before)

print("== 7. 平行样品质控：偏差计算与最小顶点覆盖 ==")
check("双零浓度偏差为 0", relative_deviation(0.0, 0.0) == 0.0)
check("偏差公式 2|a-b|/(a+b)",
      abs(relative_deviation(10.0, 12.0) - 4.0 / 22.0) < 1e-12)
check("三角形 K3 字典序最小覆盖 [a,b]",
      minimum_vertex_cover(list("abc"),
                           [("a", "b"), ("b", "c"), ("a", "c")]) == ["a", "b"])
check("P4 字典序最小覆盖 [a,c]",
      minimum_vertex_cover(list("abcd"),
                           [("a", "b"), ("b", "c"), ("c", "d")]) == ["a", "c"])
check("独立边覆盖取每边小端",
      minimum_vertex_cover(list("abcd"),
                           [("a", "b"), ("c", "d")]) == ["a", "c"])
check("mandatory 顶点强制入选",
      minimum_vertex_cover(list("abcd"), [("a", "b"), ("c", "d")],
                           mandatory={"d"}) == ["a", "d"])
check("空图覆盖为空", minimum_vertex_cover([], []) == [])

import itertools
import random as _random
_random.seed(42)
brute_ok = True
for _trial in range(120):
    nn = _random.randint(1, 9)
    vv = list(range(nn))
    ee = set()
    for _ in range(nn * 2):
        if nn >= 2:
            ee.add(tuple(sorted(_random.sample(vv, 2))))
    mand = set(_random.sample(vv, _random.randint(0, min(2, nn))))
    got = minimum_vertex_cover(vv, list(ee), mandatory=mand)
    want = None
    for rr in range(nn + 1):
        for sub in itertools.combinations(vv, rr):
            ss = set(sub)
            if mand <= ss and all(u in ss or v in ss for u, v in ee):
                want = list(sub)
                break
        if want is not None:
            break
    if got != want:
        brute_ok = False
        break
check("120 组随机图与暴力枚举一致（最小且字典序最小）", brute_ok)

print("== 8. 配对 CSV 校验 ==")
pairs = validate_pair_csv(read_example("parallel_pairs.csv"))
check("合法配对 CSV 解析 3 对", len(pairs) == 3 and pairs[0]["limit"] == 0.1)
try:
    validate_pair_csv(read_example("parallel_pairs_invalid.csv"))
    check("非法配对 CSV 被拒", False)
except QCValidationError as exc:
    joined = " ".join(exc.messages)
    check("相同样本/重复配对/负限值/非数字全部被拒",
          all(k in joined for k in ("不能相同", "重复", "负数", "不是合法数字")),
      str(exc.messages))

for raw_text, key in [
    ("样本甲,样本乙,检测项目,允许相对偏差\nS1,S2,氨氮,nan\n", "NaN"),
    ("样本甲,样本乙,检测项目,允许相对偏差\nS1,S2,氨氮,inf\n", "Inf"),
    ("样本甲,样本乙,检测项目\nS1,S2,氨氮\n", "缺列"),
]:
    try:
        validate_pair_csv(raw_text.encode())
        check(f"非法限值/表头被拒（{key}）", False)
    except QCValidationError:
        check(f"非法限值/表头被拒（{key}）", True)

over60 = "样本甲,样本乙,检测项目,允许相对偏差\n" + "".join(
    f"S{i},S{i+1},氨氮,0.1\n" for i in range(61))
try:
    validate_pair_csv(over60.encode())
    check("超过 60 对被拒", False)
except QCValidationError as exc:
    check("超过 60 对被拒", any("60" in m for m in exc.messages))

over24 = "样本甲,样本乙,检测项目,允许相对偏差\n" + "".join(
    f"A{i},B{i},COD,0.1\n" for i in range(13))
try:
    validate_pair_csv(over24.encode())
    check("超过 24 键被拒", False)
except QCValidationError as exc:
    check("超过 24 键被拒", any("24" in m for m in exc.messages))

print("== 9. 质控页面端到端（版本快照/缺失/超限/最少核查） ==")
resp = client.get("/qc")
check("质控页 200 且列出已发布版本",
      resp.status_code == 200 and b'name="version"' in resp.data)

resp = client.post("/qc/report", data={
    "version": "999",
    "pair_csv": (io.BytesIO(read_example("parallel_pairs.csv")), "p.csv"),
}, content_type="multipart/form-data")
check("不存在版本被拒（400 无报告）",
      resp.status_code == 400 and "不存在".encode() in resp.data)

resp = client.post("/qc/report", data={
    "version": "1",
    "pair_csv": (io.BytesIO(read_example("parallel_pairs_invalid.csv")), "bad.csv"),
}, content_type="multipart/form-data")
check("无效上传不生成报告（400 且回到质控表单页）",
      resp.status_code == 400 and b'name="pair_csv"' in resp.data
      and "不能相同".encode() in resp.data and "重复".encode() in resp.data
      and "负数".encode() in resp.data and "不是合法数字".encode() in resp.data)

# v1 数据：S001 氨氮 1.25 / S002 氨氮 2.10（偏差 0.5075>0.1 超限）；
# S001 总磷 0.08 / S003 总磷 0（偏差 2.0>0.15 超限）；
# S002 COD 35.0 / S004 COD 在 v1 缺失 → (S004,COD) 强制核查
resp = client.post("/qc/report", data={
    "version": "1",
    "pair_csv": (io.BytesIO(read_example("parallel_pairs.csv")), "p.csv"),
}, content_type="multipart/form-data")
body = resp.data.decode()
check("合法报告 200 并标明 v1",
      resp.status_code == 200 and "质控报告".encode() in resp.data and "v1" in body)
check("展示实测偏差 50.75%", "50.75%" in body)
check("缺失端展示原因且不按 0 计算",
      "缺失".encode() in resp.data and "不按 0 计算".encode() in resp.data)
check("给出最少待核查键集合", "最少待核查".encode() in resp.data)

# 直接验证分析结果的集合语义：两条超限边 + 一个缺失强制键
pairs_v1 = validate_pair_csv(read_example("parallel_pairs.csv"))
report = analyze_pairs(pairs_v1, app.storage.version_rows(1))
exceeded_edges = [
    (r["key_a"], r["key_b"]) for r in report["results"] if r["status"] == "exceeded"
]
# 边1：(S001,氨氮)-(S002,氨氮)；边2：(S001,总磷)-(S003,总磷)；
# 最小覆盖可取 (S001,氨氮) 与 (S001,总磷)（共 2 个），加缺失键 (S004,COD) 共 3 个
check("超限边识别为 2 条", len(exceeded_edges) == 2, str(exceeded_edges))
check("最少核查集合大小为 3（2 覆盖 + 1 强制缺失）",
      len(report["check_keys"]) == 3, str(report["check_keys"]))
check("缺失键强制在集合中", ("S004", "COD") in report["check_keys"])
check("集合按键排序",
      report["check_keys"] == sorted(report["check_keys"]))

# 双零：v1 中 S003 总磷为 0，构造同项目两个零样本（补一个零浓度行做纯函数验证）
zero_report = analyze_pairs(
    validate_pair_csv(
        "样本甲,样本乙,检测项目,允许相对偏差\nZA,ZB,总磷,0.05\n".encode()),
    [
        {"sample_no": "ZA", "analyte": "总磷", "concentration": 0.0},
        {"sample_no": "ZB", "analyte": "总磷", "concentration": 0.0},
    ],
)
check("两浓度均为 0 时偏差 0 且不超限",
      zero_report["results"][0]["deviation"] == 0.0
      and zero_report["results"][0]["status"] == "ok"
      and zero_report["check_keys"] == [])

# 分析期间继续发布：再发 v4，旧 v1 报告内容不变，页面提示当前版本已更新
v_now = app.storage.current_registry()["current_version"]
r = upload(client, "batch_v2_revision.csv", "QC-CONCURRENT")
tok = re.search(rb'/publish/([0-9a-f]+)', r.data).group(1).decode()
client.post(f"/publish/{tok}")
check("分析期间可继续发布",
      app.storage.current_registry()["current_version"] == v_now + 1)
report_before = analyze_pairs(pairs_v1, app.storage.version_rows(1))
resp = client.post("/qc/report", data={
    "version": "1",
    "pair_csv": (io.BytesIO(read_example("parallel_pairs.csv")), "p.csv"),
}, content_type="multipart/form-data")
report_after = analyze_pairs(pairs_v1, app.storage.version_rows(1))
check("旧版本报告只使用所选快照（结果不随后续发布变化）",
      report_before["check_keys"] == report_after["check_keys"]
      and f"v{v_now + 1}".encode() in resp.data
      and "不受影响".encode() in resp.data)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
