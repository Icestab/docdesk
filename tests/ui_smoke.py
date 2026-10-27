#!/usr/bin/env python3
"""ui.html 的冒烟测试：在 quickjs 里用假 DOM 跑上传/粘贴两条路径。

    pip install quickjs
    python3 tests/ui_smoke.py

没有浏览器、没有构建步骤，只验证 JS 不会在运行时抛错（比如 TDZ、拼错函数名）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import quickjs

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "ui.html").read_text(encoding="utf-8")
JS = re.search(r"<script>(.*)</script>", HTML, re.S).group(1)

STUB = r"""
const LOG = [];
globalThis.window = {scrollTo(){}};
function El(id){
  const o = {
    id, textContent:'', style:{}, dataset:{}, open:false, _h:'',
    get innerHTML(){return o._h},
    set innerHTML(v){o._h=String(v); LOG.push('html:'+String(v).replace(/<[^>]*>/g,'').slice(0,80))},
    classList:{add(){},remove(){},contains(){return false}},
    appendChild(c){LOG.push('append:'+(c&&c.className||'')); return c},
    remove(){LOG.push('remove')},
    addEventListener(){}, querySelector(){return El('q')}, querySelectorAll(){return []},
    closest(){return null}, scrollIntoView(){}, focus(){}, click(){}, setAttribute(){},
    value:'', disabled:false, files:[]
  };
  return o;
}
const document = {
  querySelector(s){ return El(s) },
  querySelectorAll(){ return [] },
  createElement(t){ const e=El(t); e.className=''; return e },
  addEventListener(){}, body:{innerHTML:'', appendChild(){}, scrollHeight:100}
};
function setTimeout0(fn){ fn() }
// 浏览器里才有，测试环境补上
globalThis.TextEncoder = class {
  encode(s){ const a=[]; for (let i=0;i<s.length;i++) a.push(s.charCodeAt(i)&255); return new Uint8Array(a) }
};
globalThis.btoa = () => 'AAAA';
"""

HARNESS = r"""
globalThis.FileReader = class {
  readAsDataURL(file){ this.result = 'data:x;base64,AAAA'; setTimeout0(()=>this.onload()); }
};
globalThis.fetch = async function(){
  return {ok:true, json: async ()=>({
    docs:[{id:1,name:'测试.pdf',pages:3,tokens:1234}], total_tokens:1234,
    added:{name:'测试.pdf',pages:3,tokens:1234,ocr:2,scanned:[3]}})};
};
globalThis.__run = async function(){
  await upload([{name:'测试.pdf', size:1000}]);
  await addTextDoc('粘贴.txt', '客户：某某公司\\n金额：86 万元');
  return LOG.join(' | ');
};
"""


def run() -> str:
    ctx = quickjs.Context()
    ctx.eval(STUB)
    ctx.eval(JS)
    ctx.eval(HARNESS)
    ctx.eval("__run().then(r=>{globalThis.__result=r}, e=>{globalThis.__error=String(e)})")
    for _ in range(500):
        if not ctx.execute_pending_job():
            break
        if ctx.eval("globalThis.__result || globalThis.__error || ''"):
            break
    err = ctx.eval("globalThis.__error || ''")
    if err:
        raise AssertionError(f"JS 抛异常: {err}")
    return ctx.eval("globalThis.__result || '(timeout)'")


if __name__ == "__main__":
    log = run()
    bad = [w for w in ("is not initialized", "is not defined", "undefined is not") if w in log]
    print(log)
    if bad:
        print("\nFAIL:", ", ".join(bad))
        sys.exit(1)
    if "已载入" not in log:
        print("\nFAIL: 上传成功后没有渲染结果")
        sys.exit(1)
    print("\nOK")
