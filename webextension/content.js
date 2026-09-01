/**
 * 学习通做题辅助 content script。
 * 配合本地主程序(fuck_DXSTJ)工作:
 * - 每 2 秒轮询本地桥 http://127.0.0.1:9876/config,主程序点「开始」后 enabled=true
 * - 扫描本 frame 的题目(插件以 all_frames 注入,天然覆盖做题页 iframe)
 * - 逐题 POST /solve 获取答案并点击;全部答完滚动找新题,滚不动则报告完成
 *
 * 题目抽取双策略:
 * 1) 容器选择器(.TiMu 等)命中 N 个容器 → 每容器一题(标准版式)
 * 2) 命中 ≤1 个但页面 radio/checkbox ≥4 → 按选项行聚类兜底(不依赖类名):
 *    同 name 的 radio 必属同题;同行父元素/垂直间距近的选项行聚为一题;
 *    题干用 Range 取上一题末行到本题首行之间的文本
 */
(() => {
  'use strict';
  const API = 'http://127.0.0.1:9876';
  let abort = false;
  let working = false;
  let diagnosed = false;

  // 抽取缓存:clickOption 直接引用行元素,避免二次推导错位
  const cache = { items: [] };

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

  function diagnose(msg) {
    if (diagnosed) return;
    diagnosed = true;
    report('log', { msg });
  }

  // ---------- 选项行工具 ----------

  function rowOf(inp) {
    return inp.closest('label, li, div, p') || inp.parentElement;
  }

  function labelOf(text, qtype, i) {
    const m = text.match(/^([A-H])[.、．,，:：\s]/);
    if (m) return m[1];
    if (qtype === 'judge') {
      if (/[对正确√]/.test(text)) return '对';
      if (/[错误×]/.test(text)) return '错';
      return null;
    }
    return String.fromCharCode(65 + i);
  }

  function qtypeFromText(t) {
    if (t.includes('判断')) return 'judge';
    if (t.includes('多选')) return 'multiple';
    return 'single';
  }

  // ---------- 策略1:容器选择器 ----------

  function extractByContainers() {
    const SEL = '.TiMu, .singleQuesId, .question, [class*="TiMu"], [class*="timu"], [class*="singleQuesId"]';
    const containers = [...document.querySelectorAll(SEL)]
      .filter(el => (el.innerText || '').trim().length > 10);
    return containers.map(c => {
      const full = c.innerText || '';
      const typeEl = c.querySelector('.colorShallow, [class*="type"], [class*="Type"]');
      const qtype = qtypeFromText(
        ((typeEl ? typeEl.innerText : '') + full.slice(0, 60)));

      const rows = [...c.querySelectorAll('input[type=radio], input[type=checkbox]')]
        .map(inp => ({ row: rowOf(inp), text: (rowOf(inp).innerText || '').trim() }));
      let labels = rows.map((r, i) => labelOf(r.text, qtype, i));

      const stemEl = c.querySelector('.Cy_txt, .qtstem, [class*="stem"], [class*="Stem"]');
      let stem = stemEl ? stemEl.innerText.trim() : '';
      if (!stem) {
        stem = full.split('\n').filter(line => {
          const s = line.trim();
          if (!s) return false;
          return !rows.some(r => r.text.startsWith(s.slice(0, 8))) &&
                 !/^[A-H][.、．,，:：\s]/.test(s);
        }).join(' ').slice(0, 300);
      }
      const answered = !!c.querySelector(
        'input[type=radio]:checked, input[type=checkbox]:checked, [data-xxt-done]');
      return {
        qtype, answered, stem: stem.replace(/\s+/g, ' ').trim(),
        rows, labels,
      };
    });
  }

  // ---------- 策略2:题号锚点切分(不依赖容器类名) ----------

  // 题号锚点:文本形如 "1. (单选题)" / "12.(多选题)" / "26.(判断题)"
  function findQuestionAnchors() {
    const out = [];
    const els = document.querySelectorAll(
      'div, p, span, i, em, li, label, h1, h2, h3, h4, strong, b');
    for (const el of els) {
      if (el.children.length > 3) continue;   // 锚点是小元素,大容器会误命中
      const t = (el.innerText || '').trim();
      if (!t || t.length > 60) continue;
      if (/^\d{1,3}\s*[.、．)]?\s*[（(]?\s*(单选题|多选题|判断题|填空题|简答题)/.test(t)) {
        out.push(el);
      }
    }
    // 父子同匹配时只保留最内层元素
    return out.filter(el => !out.some(o => o !== el && el.contains(o)));
  }

  function extractByInputs() {
    const inputs = [...document.querySelectorAll('input[type=radio], input[type=checkbox]')];
    if (inputs.length < 4) return null;
    const rows = inputs.map(inp => ({ inp, row: rowOf(inp) }));
    // 文档顺序
    rows.sort((a, b) =>
      (a.row.compareDocumentPosition(b.row) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);

    // 分组:题号锚点优先(精确),无锚点回退间距聚类
    let groups = null;
    const anchors = findQuestionAnchors();
    if (anchors.length >= 2) {
      groups = anchors.map(() => []);
      for (const r of rows) {
        let ai = -1;
        for (let k = 0; k < anchors.length; k++) {
          if (anchors[k].compareDocumentPosition(r.row) &
              Node.DOCUMENT_POSITION_FOLLOWING) ai = k;
        }
        if (ai >= 0) groups[ai].push(r);
      }
      groups = groups.filter(g => g.length);
      if (groups.length >= 2) {
        diagnose(`题号锚点切分:识别到 ${groups.length} 题`);
      } else {
        groups = null;   // 锚点没对上选项行,回退聚类
      }
    }
    if (!groups) {
      // 聚类兜底:同 name(radio)/同父元素/垂直间距<40px → 同题
      groups = [];
      let cur = null;
      for (let i = 0; i < rows.length; i++) {
        const r = rows[i];
        let same = false;
        if (cur && i > 0) {
          const p = rows[i - 1];
          if (r.inp.type === 'radio' && p.inp.type === 'radio' &&
              r.inp.name && r.inp.name === p.inp.name) same = true;
          else if (r.row.parentElement === p.row.parentElement) same = true;
          else {
            const gap = Math.abs(
              r.row.getBoundingClientRect().top -
              p.row.getBoundingClientRect().bottom);
            if (gap < 40) same = true;
          }
        }
        if (same && cur) cur.push(r);
        else { cur = [r]; groups.push(cur); }
      }
      if (groups.length < 2) return null;
      diagnose(`选项行聚类兜底:识别到 ${groups.length} 题`);
    }

    // 题干:Range 取上一组末行到本组首行之间的文本(含题号锚点行)
    const items = [];
    for (let g = 0; g < groups.length; g++) {
      const grp = groups[g];
      let stem = '';
      try {
        const range = document.createRange();
        if (g === 0) range.setStart(document.body, 0);
        else range.setStartAfter(groups[g - 1][groups[g - 1].length - 1].row);
        range.setEndBefore(grp[0].row);
        stem = range.toString().replace(/\s+/g, ' ').trim();
      } catch (e) { /* 跨边界异常则空题干 */ }
      // 题型从未去前缀的题干判断(含"单选题/多选题/判断题"字样),再去掉题号前缀
      const qtype = qtypeFromText(stem.slice(0, 60));
      stem = stem.replace(/^\d{1,3}\s*[.、．)]?\s*[（(][^)）]*[)）]\s*/, '');
      const texts = grp.map(r => (r.row.innerText || '').trim());
      const labels = texts.map((t, i) => labelOf(t, qtype, i));
      const answered = grp.some(r => r.inp.checked) ||
        grp.some(r => r.row.hasAttribute('data-xxt-done'));
      items.push({
        qtype, answered,
        stem: stem.slice(-200),
        rows: grp.map(r => ({ row: r.row, text: texts[grp.indexOf(r)] })),
        labels,
      });
    }
    return items;
  }

  function extract() {
    let items = extractByContainers();
    const nCont = items.length;
    if (nCont <= 1) {
      const fb = extractByInputs();
      if (fb && fb.length > nCont) {
        diagnose(`容器选择器仅命中 ${nCont} 个,启用选项行聚类兜底:识别到 ${fb.length} 题`);
        items = fb;
      } else {
        diagnose(`容器选择器命中 ${nCont} 个且兜底未生效,可能页面仍在加载`);
      }
    }
    cache.items = items;
    return items.map((it, idx) => ({
      idx, qtype: it.qtype, stem: it.stem,
      labels: it.labels, texts: it.rows.map(r => r.text),
      answered: it.answered,
    }));
  }

  function optsOf(it) {
    const opts = {};
    const item = cache.items[it.idx];
    if (!item) return opts;
    for (let i = 0; i < item.labels.length; i++) {
      if (item.labels[i] && !(item.labels[i] in opts)) {
        opts[item.labels[i]] = item.rows[i].text;
      }
    }
    return opts;
  }

  // ---------- 点击选项(直接使用抽取缓存的行元素) ----------

  function clickOption(qi, label) {
    const item = cache.items[qi];
    if (!item) return 'no-item';

    let target = null;
    for (let i = 0; i < item.labels.length; i++) {
      if (item.labels[i] === label) { target = item.rows[i].row; break; }
    }
    if (!target) {
      for (let i = 0; i < item.rows.length; i++) {
        const t = item.rows[i].text;
        if (label === '对' && /[对正确√]/.test(t)) { target = item.rows[i].row; break; }
        if (label === '错' && /[错误×]/.test(t)) { target = item.rows[i].row; break; }
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
    // 3) 验证:input checked 或行元素可见状态变化
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

  // ---------- 滚动找新题(步进,不狂飙) ----------

  function scrollPage() {
    // 找最大的可滚动容器,每次滚动约 0.8 屏(之前一次拉到底,视觉上狂滑且跳题)
    let best = null;
    for (const el of document.querySelectorAll('div')) {
      if (el.scrollHeight > el.clientHeight + 100 && el.clientHeight > 200) {
        if (!best || (el.scrollHeight - el.clientHeight) > (best.scrollHeight - best.clientHeight)) {
          best = el;
        }
      }
    }
    let moved = false;
    const step = Math.floor(window.innerHeight * 0.8);
    if (window.scrollY + window.innerHeight < document.documentElement.scrollHeight - 50) {
      window.scrollBy(0, step);
      moved = true;
    }
    if (best && best.scrollTop + best.clientHeight < best.scrollHeight - 50) {
      best.scrollTop += step;
      moved = true;
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
