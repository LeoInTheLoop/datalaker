import os, sys, time, uuid
sys.path.insert(0, '/Users/lingyukong/Documents/GitHub/datalaker')
DB="/tmp/dl_esc.db"
for s in ("","-wal","-shm"):
    if os.path.exists(DB+s): os.remove(DB+s)
os.environ.update(DATASTEWARD_DB=DB); os.environ.pop("DATASTEWARD_DSN", None)
from plugins.datasteward_gate.approvals import open_store, action_hash
st = open_store(readonly=False)
ok,bad=[],[]
def chk(n,c,d=""):
    (ok if c else bad).append(n); print(f"  {'PASS' if c else 'FAIL'}  {n}"+(f"  [{d}]" if d else ""))

print("\n=== 超时升级 → 优雅放弃 ===\n")
# 造不同年龄的事项
ages = {"fresh":1, "d4":4*24, "d7":7*24, "d13":13*24}
ids={}
for k,h in ages.items():
    i=str(uuid.uuid4()); ids[k]=i
    st.db.execute("INSERT INTO approvals (id,run_id,action_hash,tool_name,args_json,"
        "approver,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (i,"r",action_hash("ingest_table",{"t":k}),"ingest_table","{}","owner",
         time.time()-h*3600, time.time()+999999))
st.db.commit()
chk("初始在办计数", st.open_count()==4, f"{st.open_count()} 件")

sys.argv=["escalate"]
sys.path.insert(0,'/Users/lingyukong/Documents/GitHub/datalaker/ops')
import escalate
n = escalate.main()
lv = dict((r[0], r[4]) for r in st.stale_items())
chk("1 小时的不升级", lv.get(ids["fresh"])==0)
chk("4 天的升到 L1（催办）", lv.get(ids["d4"])==1)
chk("7 天的升到 L2（Owner）", lv.get(ids["d7"])==2)
ab=[r[0] for r in st.abandoned()]
chk("13 天的标记 ABANDONED", ids["d13"] in ab)
chk("放弃后退出在办队列", st.open_count()==3, f"{st.open_count()} 件")
chk("放弃不是删除（记录仍在）",
    st.db.execute("SELECT count(*) FROM approvals WHERE id=?",(ids["d13"],)).fetchone()[0]==1)
ev=[k for _,k,_ in st.events(ids["d13"])]
chk("event log 保留放弃原因", "ABANDONED" in ev, ",".join(ev))
n2 = escalate.main()
chk("重复运行幂等（不重复升级）", n2==0, f"第二次处理 {n2} 项")
print(f"\n结果: {len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)

# 周报能读账本
st.db.execute("INSERT INTO query_ledger (ts,source_id,sql_text,rows_out,duration_ms,status)"
              " VALUES (?,?,?,?,?,?)", (time.time(),"olist","SELECT 1",5,12.3,"OK"))
st.db.execute("INSERT INTO usage_ledger (ts,model,prompt_tokens,output_tokens,cost_usd)"
              " VALUES (?,?,?,?,?)", (time.time(),"qwen3.7-plus",1200,300,0.0))
st.db.commit()
sys.path.insert(0,'/Users/lingyukong/Documents/GitHub/datalaker/ops')
import importlib.util
spec=importlib.util.spec_from_file_location("wr","/Users/lingyukong/Documents/GitHub/datalaker/ops/weekly-report.py")
wr=importlib.util.module_from_spec(spec); spec.loader.exec_module(wr)
rep = wr.build()
print()
print("  PASS  周报四段齐全" if all(x in rep for x in ("本周完成","卡在谁那里","需要你决策","系统开销")) else "  FAIL  周报缺段")
print("  PASS  周报能读账本" if "模型调用 1 次" in rep else f"  FAIL  周报账本: {[l for l in rep.split(chr(10)) if '模型' in l]}")
print("  PASS  周报含已放弃项" if "已知阻塞项" in rep else "  FAIL  缺已放弃项")
