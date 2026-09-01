/**
 * 学习通做题辅助 content script。
 * 配合本地主程序(fuck_DXSTJ)工作:
 * - 每 2 秒轮询本地桥 http://127.0.0.1:9876/config,主程序点「开始」后 enabled=true
 * - 扫描本 frame 的题目(插件以 all_frames 注入,天然覆盖做题页 iframe)
 * - 逐题 POST /solve 获取答案并点击;全部答完滚动找新题,滚不动则报告完成
 */
(() => {
  'use strict';
  const API = 'http://127.0.0.1:9876';
  let abort = false;
  let working = false;

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const rnd = (a, b) => a + Math.random() * (b - a);

  async function api(path, body) {
    const opt = body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body) }
      : { cache: 'no-store' };
    const res = await fetch(API + path, opt);
    return res.json();
  }

  function report(event, data) {
    return api('/report', { event, data }).catch(() => {});
  }

  // ---------- 题目抽取(与主程序 CDP 方案的 EXTRACT_JS 同构) ----------

  function extract() {
    const SEL = '.TiMu, .question, [class*="TiMu"], [class*="timu"]';
    const containers = [...document.querySelectorAll(SEL)]
      .filter(el => (el.innerText || '').trim().length > 10);
    return containers.map((c, idx) => {
      const full = c.innerText || '';

      const typeEl = c.querySelector('.colorShallow, [class*="type"], [class*="Type"]');
      let qtype = 'single';
      const t = ((typeEl ? typeEl.innerText : '') + full.slice(0, 60));
      if (t.includes('判断')) qtype = 'judge';
      else if (t.includes('多选')) qtype = 'multiple';

      const texts = [], els = [];
      const inputs = c.querySelectorAll('input[type=radio], input[type=checkbox]');
      if (inputs.length) {
        for (const inp of inputs) {
          const row = inp.closest('label, li, div, p') || inp.parentElement;
          if (!row) continue;
          els.push(row); texts.push((row.innerText || '').trim());
        }
      } else {
        const rows = [...c.querySelectorAll('li, label, div, p')]
          .filter(el => el.children.length <= 2 &&
            /^[A-H][.、．,，:：\s]/.test((el.innerText || '').trim()));
        for (const row of rows) { els.push(row); texts.push((row.innerText || '').trim()); }
      }

      const labels = [];
      for (let i = 0; i < texts.length; i++) {
        const m = texts[i].match(/^([A-H])[.、．,，:：\s]/);
        let lb;
        if (m) lb = m[1];
        else if (qtype === 'judge') {
          if (/[对正确√]/.test(texts[i])) lb = '对';
          else if (/[错误×]/.test(texts[i])) lb = '错';
          else lb = null;
        } else lb = String.fromCharCode(65 + i);
        labels.push(lb);
      }

      // 已答状态:原生 checked 或我们点击成功后打标的 data-xxt-done
      // (学习通部分版式无 input 元素,靠点击标记避免死循环)
      const answered = !!c.querySelector(
        'input[type=radio]:checked, input[type=checkbox]:checked, [data-xxt-done]');

      const stemEl = c.querySelector('.Cy_txt, .qtstem, [class*="stem"], [class*="Stem"]');
      let stem = stemEl ? stemEl.innerText.trim() : '';
      if (!stem) {
        stem = full.split('\n').filter(line => {
          const s = line.trim();
          if (!s) return false;
          return !texts.some(t => t.startsWith(s.slice(0, 8))) &&
                 !/^[A-H][.、．,，:：\s]/.test(s);
        }).join(' ').slice(0, 300);
      }
      return {
        idx, qtype,
        stem: stem.replace(/\s+/g, ' ').trim(),
        labels, texts, answered,
      };
    });
  }

  function optsOf(it) {
    const opts = {};
    for (let i = 0; i < it.labels.length; i++) {
      if (it.labels[i] && !(it.labels[i] in opts)) opts[it.labels[i]] = it.texts[i];
    }
    return opts;
  }

  // ---------- 点击选项(与 CLICK_JS 同构) ----------

  function clickOption(qi, label) {
    const SEL = '.TiMu, .question, [class*="TiMu"], [class*="timu"]';
    const containers = [...document.querySelectorAll(SEL)]
      .filter(el => (el.innerText || '').trim().length > 10);
    const c = containers[qi];
    if (!c) return 'no-container';

    const texts = [], els = [];
    const inputs = c.querySelectorAll('input[type=radio], input[type=checkbox]');
    if (inputs.length) {
      for (const inp of inputs) {
        const row = inp.closest('label, li, div, p') || inp.parentElement;
        if (!row) continue;
        els.push(row); texts.push((row.innerText || '').trim());
      }
    } else {
      const rows = [...c.querySelectorAll('li, label, div, p')]
        .filter(el => el.children.length <= 2 &&
          /^[A-H][.、．,，:：\s]/.test((el.innerText || '').trim()));
      for (const row of rows) { els.push(row); texts.push((row.innerText || '').trim()); }
    }

    let target = null;
    for (let i = 0; i < texts.length; i++) {
      const m = texts[i].match(/^([A-H])[.、．,，:：\s]/);
      if (m && m[1] === label) { target = els[i]; break; }
    }
    if (!target) {
      for (let i = 0; i < texts.length; i++) {
        if (label === '对' && /[对正确√]/.test(texts[i])) { target = els[i]; break; }
        if (label === '错' && /[错误×]/.test(texts[i])) { target = els[i]; break; }
      }
    }
    if (!target) return 'no-option';

    target.scrollIntoView({ block: 'center' });
    const r = target.getBoundingClientRect();
    const x = r.left + r.width * (0.3 + Math.random() * 0.4);
    const y = r.top + r.height * (0.3 + Math.random() * 0.4);

    const inp = target.querySelector('input[type=radio], input[type=checkbox]');
    // 1) 原生点击 input 最可靠(直接置 checked)
    if (inp && !inp.checked) {
      inp.click();
      if (inp.checked) {
        target.setAttribute('data-xxt-done', '1');
        return 'ok';
      }
    }
    // 2) 派发鼠标事件序列(页面监听 click 的版式)
    const clsBefore = target.className;
    for (const type of ['mousedown', 'mouseup', 'click']) {
      target.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, view: window,
        clientX: x, clientY: y,
      }));
    }
    // 3) 验证:input checked 或行元素 class/style 变化
    const verified = (inp && inp.checked) ||
                     target.className !== clsBefore ||
                     target.querySelector('[class*="check"], [class*="Check"], [class*="selected"], [class*="active"]');
    if (!verified) {
      // 无 input 且无可见状态变化:标记已处理防止死循环,但提示人工检查
      target.setAttribute('data-xxt-done', '1');
      return 'unverified';
    }
    target.setAttribute('data-xxt-done', '1');
    return 'ok';
  }

  // ---------- 滚动找新题 ----------

  function scrollPage() {
    let moved = false;
    const before = window.scrollY || document.documentElement.scrollTop || 0;
    window.scrollBy(0, Math.floor(window.innerHeight * 0.8));
    const after = window.scrollY || document.documentElement.scrollTop || 0;
    if (after !== before) moved = true;
    for (const el of document.querySelectorAll('div')) {
      if (el.scrollHeight > el.clientHeight + 100 && el.clientHeight > 200) {
        const b = el.scrollTop;
        el.scrollTop = el.scrollHeight;
        if (el.scrollTop !== b) moved = true;
      }
    }
    return moved;
  }

  // ---------- 主流程 ----------

  async function run(cfg) {
    let idle = 0;
    const attempts = {};   // 题目重试次数(防 LLM 死循环调用)
    while (!abort) {
      const items = extract();
      if (!items.length) {
        // 本 frame 没有题目(通常是外层框架),不参与完成判定
        return;
      }
      let pending = 0;
      for (const it of items) {
        if (abort) return;
        if (it.answered) continue;
        const key = it.idx + '|' + it.stem.slice(0, 24);
        attempts[key] = (attempts[key] || 0) + 1;
        if (attempts[key] > 3) {
          await report('fail', { msg: `题目${it.idx + 1} 点击后未生效(已重试3次),请人工检查` });
          continue;
        }
        pending++;
        const opts = optsOf(it);
        await report('question', { qtype: it.qtype, stem: it.stem, options: opts });
        const r = await api('/solve', {
          number: it.idx + 1, qtype: it.qtype, stem: it.stem, options: opts,
        }).catch(() => null);
        if (!r || !r.ok) {
          await report('fail', { msg: `题目${it.idx + 1} 求解失败: ${r ? r.error : '桥服务无响应'}` });
          continue;
        }
        await report('answer', { number: it.idx + 1, answer: r.answer });
        if (!cfg.dry_run) {
          for (const lb of r.answer) {
            const ret = clickOption(it.idx, lb);
            if (ret === 'unverified') {
              await report('log', { msg: `题目${it.idx + 1} 选项 ${lb} 点击后未检出选中效果,请人工检查` });
            } else if (ret !== 'ok') {
              await report('log', { msg: `选项 ${lb} 点击失败: ${ret}` });
            }
            await sleep(rnd(cfg.opt_delay[0], cfg.opt_delay[1]) * 1000);
          }
        }
        await sleep(rnd(cfg.q_delay[0], cfg.q_delay[1]) * 1000);
      }
      if (pending === 0) {
        const moved = scrollPage();
        if (!moved) {
          idle++;
          if (idle >= 2) {
            await report('frame-done', { msg: '本页题目已全部答完且无法再滚动' });
            return;
          }
        } else {
          idle = 0;
        }
        await sleep(1500);
      } else {
        await sleep(800);
      }
    }
  }

  async function main() {
    // 注入即上报一次,主程序据此确认插件在页面中生效
    await report('hello', { url: location.href });
    while (!abort) {
      try {
        const cfg = await api('/config');
        if (!cfg.enabled) { abort = true; break; }
        if (!working) {
          working = true;
          run(cfg).finally(() => { working = false; });
        }
      } catch (e) { /* 本地桥未启动,静默等待 */ }
      await sleep(2000);
    }
  }
  main();
})();
