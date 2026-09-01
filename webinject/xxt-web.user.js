// ==UserScript==
// @name         学习通网页版自动做题(fuck_DXSTJ)
// @namespace    fuck_DXSTJ
// @version      0.1.0
// @description  网页版学习通自动做题:DOM 读题 → OpenAI 兼容 API 作答 → 模拟点击。免 OCR、免窗口焦点。
// @author       fuck_DXSTJ
// @match        *://*.chaoxing.com/*
// @match        *://*.edu.cn/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @connect      *
// @noframes     false
// ==/UserScript==

/*
 * 使用方法:
 * 1. 浏览器安装 Tampermonkey 扩展
 * 2. 新建脚本,粘贴本文件全部内容保存
 * 3. 点击 Tampermonkey 图标 → 本脚本菜单 → "设置 API",依次填写
 *    Base URL(如 https://opencode.ai/zen/go/v1)、API Key、模型名
 *    (与桌面版 config.yaml 的 llm 段一致即可)
 * 4. 打开学习通网页版作业/考试页面,右下角出现"自动做题"面板,点击开始
 *
 * 注意:仅在题目直接渲染在 chaoxing.com 域(含 iframe)下生效;
 * 题目在第三方域 iframe 中时需为该域补充 @match。
 */

(function () {
    'use strict';

    // ---------------- 配置 ----------------

    const DEFAULT_CFG = {
        base_url: '',   // 例: https://opencode.ai/zen/go/v1
        api_key: '',
        model: '',
        temperature: 0.1,
        q_delay: [3, 8],   // 每题间隔随机秒数(防检测)
        opt_delay: [0.5, 1.5], // 选项间点击随机秒数
    };

    function loadCfg() {
        const saved = GM_getValue('cfg', '{}');
        try { return Object.assign({}, DEFAULT_CFG, JSON.parse(saved)); }
        catch (e) { return Object.assign({}, DEFAULT_CFG); }
    }
    function saveCfg(cfg) { GM_setValue('cfg', JSON.stringify(cfg)); }

    const cfg = loadCfg();

    GM_registerMenuCommand('设置 API', promptApiCfg);
    GM_registerMenuCommand('重置配置', () => { saveCfg(DEFAULT_CFG); alert('已重置'); });

    function promptApiCfg() {
        const base = prompt('Base URL(OpenAI 兼容,含 /v1):', cfg.base_url || 'https://opencode.ai/zen/go/v1');
        if (base === null) return;
        cfg.base_url = base.trim().replace(/\/+$/, '');
        const key = prompt('API Key:', cfg.api_key);
        if (key === null) return;
        cfg.api_key = key.trim();
        const model = prompt('模型名:', cfg.model);
        if (model === null) return;
        cfg.model = model.trim();
        saveCfg(cfg);
        alert('已保存。如已开始做题,新配置下一题生效。');
    }

    // ---------------- 工具 ----------------

    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const rand = (a, b) => a + Math.random() * (b - a);

    function gmFetchJson(url, headers, body, timeout = 60000) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: 'POST', url, headers,
                data: JSON.stringify(body), timeout,
                onload: (res) => {
                    try {
                        if (res.status < 200 || res.status >= 300)
                            return reject(new Error(`HTTP ${res.status}: ${res.responseText.slice(0, 200)}`));
                        resolve(JSON.parse(res.responseText));
                    } catch (e) { reject(e); }
                },
                onerror: (e) => reject(new Error('网络错误: ' + (e.error || ''))),
                ontimeout: () => reject(new Error('请求超时')),
            });
        });
    }

    // ---------------- LLM(与桌面版 solver.py 同一套 prompt/解析) ----------------

    const SYSTEM_PROMPT = `你是一个答题助手。根据题目和选项选出正确答案。

必须严格遵守:
1. 只输出一个 JSON 对象,不要输出任何其他内容(不要markdown代码块标记)。
2. 单选题格式: {"answer": "A"}
3. 多选题格式: {"answer": ["A", "B"]}(选项按字母顺序)
4. 判断题格式: {"answer": "对"} 或 {"answer": "错"}
5. answer 中的选项字母必须是大写,且必须是题目中实际存在的选项。
6. 如果完全无法确定,选择你认为最可能的一个,不要留空。`;

    async function llmSolve(question) {
        const body = {
            model: cfg.model,
            temperature: cfg.temperature,
            messages: [
                { role: 'system', content: SYSTEM_PROMPT },
                { role: 'user', content: question.promptText },
            ],
        };
        const data = await gmFetchJson(
            cfg.base_url + '/chat/completions',
            { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg.api_key },
            body
        );
        const reply = data?.choices?.[0]?.message?.content;
        if (!reply) throw new Error('LLM 返回为空: ' + JSON.stringify(data).slice(0, 200));
        return parseAnswer(reply, question);
    }

    function extractJson(reply) {
        let m = reply.match(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/);
        if (m) reply = m[1];
        m = reply.match(/\{[\s\S]*\}/);
        if (!m) throw new Error('回复中无 JSON: ' + reply.slice(0, 100));
        return JSON.parse(m[0]);
    }

    function parseAnswer(reply, q) {
        const data = extractJson(reply);
        let raw = data.answer;
        if (raw === null || raw === undefined) throw new Error('JSON 无 answer 字段');

        let labels;
        if (typeof raw === 'string') {
            raw = raw.trim();
            if (q.qtype !== 'judge' && raw.length > 1 && /^[A-F]+$/i.test(raw))
                labels = raw.toUpperCase().split('');
            else labels = [raw];
        } else if (Array.isArray(raw)) {
            labels = raw.map(x => String(x).trim());
        } else throw new Error('answer 类型非法');

        const valid = [];
        for (const lb of labels) {
            const up = lb.toUpperCase();
            if (q.qtype === 'judge') {
                if (q.optionTexts.some(t => t.includes(lb))) valid.push(lb);
                else if (['对', '正确', '√', 'T', 'TRUE'].includes(up) || lb === 'true' || lb === 'True') valid.push('对');
                else if (['错', '错误', '×', 'F', 'FALSE'].includes(up) || lb === 'false' || lb === 'False') valid.push('错');
            } else if (q.optionLabels.includes(up)) {
                valid.push(up);
            }
        }
        if (!valid.length) throw new Error(`答案 ${JSON.stringify(labels)} 未命中任何有效选项`);
        return [...new Set(valid)];
    }

    // ---------------- DOM 题目抽取(多选择器兜底) ----------------

    const Q_CONTAINER_SEL = '.TiMu, .question, [class*="TiMu"], [class*="timu"]';

    function detectType(text) {
        if (text.includes('判断')) return 'judge';
        if (text.includes('多选')) return 'multiple';
        return 'single';
    }

    function extractQuestion(container) {
        const q = {
            container,
            qtype: 'single',
            stem: '',
            promptText: '',
            optionLabels: [],   // ['A','B',...] 或 ['对','错']
            optionTexts: [],    // 每个选项的完整文本
            optionEls: [],      // 可点击元素
        };

        const fullText = container.innerText || '';

        // 题型:优先容器内的题型标记,退化为全文检索
        const typeEl = container.querySelector('.colorShallow, [class*="type"], [class*="Type"]');
        q.qtype = detectType((typeEl ? typeEl.innerText : '') + fullText.slice(0, 60));

        // 题干:优先已知选择器;退化 = 容器文本去掉选项行
        const stemEl = container.querySelector('.Cy_txt, .qtstem, [class*="stem"], [class*="Stem"]');
        let stem = stemEl ? stemEl.innerText.trim() : '';

        // 选项:1) 带 radio/checkbox 的行
        const inputs = container.querySelectorAll('input[type=radio], input[type=checkbox]');
        if (inputs.length) {
            for (const inp of inputs) {
                const row = inp.closest('label, li, div, p') || inp.parentElement;
                if (!row) continue;
                const text = (row.innerText || '').trim();
                q.optionEls.push(row);
                q.optionTexts.push(text);
            }
        } else {
            // 2) 正则扫文本行:A. xxx / A、xxx / A xxx
            const rows = [...container.querySelectorAll('li, label, div, p')]
                .filter(el => el.children.length <= 2 && /^[A-H][.、．,，:：\s]/.test((el.innerText || '').trim()));
            for (const row of rows) {
                q.optionEls.push(row);
                q.optionTexts.push(row.innerText.trim());
            }
        }

        // 归一化选项标签
        for (let i = 0; i < q.optionTexts.length; i++) {
            const t = q.optionTexts[i];
            const m = t.match(/^([A-H])[.、．,，:：\s]/);
            let label;
            if (m) label = m[1];
            else if (q.qtype === 'judge') {
                if (/[对正确√]|true/i.test(t)) label = '对';
                else if (/[错误×]|false/i.test(t)) label = '错';
                else label = null;
            } else label = String.fromCharCode(65 + i);  // 无字母则按序号补
            q.optionLabels.push(label);
        }

        // 题干兜底:从全文剔除选项行
        if (!stem) {
            const optJoined = q.optionTexts.map(t => t.slice(0, 12)).join('|');
            stem = fullText.split('\n').filter(line => {
                const s = line.trim();
                if (!s) return false;
                return !q.optionTexts.some(t => t.startsWith(s.slice(0, 8))) &&
                       !/^[A-H][.、．,，:：\s]/.test(s);
            }).join(' ').slice(0, 300);
        }
        q.stem = stem.replace(/\s+/g, ' ').trim();

        // 组装 prompt(与桌面版 Question.to_prompt_text 一致)
        let prompt = '';
        if (q.qtype === 'judge') {
            prompt = `判断题:${q.stem}\n请判断对错。`;
        } else {
            const type = q.qtype === 'multiple' ? '多选题' : '单选题';
            const opts = q.optionTexts.map((t, i) => `${q.optionLabels[i]}. ${t}`).join('\n');
            prompt = `${type}:${q.stem}\n${opts}`;
        }
        q.promptText = prompt;
        return q;
    }

    // ---------------- 作答 ----------------

    function findOptionEl(q, label) {
        for (let i = 0; i < q.optionLabels.length; i++) {
            if (q.optionLabels[i] === label) return q.optionEls[i];
        }
        // 判断题兜底:按文本找
        if (q.qtype === 'judge') {
            const idx = q.optionTexts.findIndex(t =>
                (label === '对' && /[对正确√]/.test(t)) ||
                (label === '错' && /[错误×]/.test(t)));
            if (idx >= 0) return q.optionEls[idx];
        }
        return null;
    }

    function clickEl(el) {
        // 派发真实鼠标事件序列,兼容仅监听 mousedown/mouseup 的前端
        const r = el.getBoundingClientRect();
        const x = r.left + r.width * rand(0.3, 0.7);
        const y = r.top + r.height * rand(0.3, 0.7);
        for (const type of ['mousedown', 'mouseup', 'click']) {
            el.dispatchEvent(new MouseEvent(type, {
                bubbles: true, cancelable: true, view: window,
                clientX: x, clientY: y,
            }));
        }
        const inp = el.querySelector('input[type=radio], input[type=checkbox]');
        if (inp && !inp.checked) inp.click();
    }

    function isAnswered(q) {
        const inp = q.container.querySelector('input[type=radio]:checked, input[type=checkbox]:checked');
        if (inp) return true;
        // 无 input 的页面(部分判断题):检测选中样式
        return q.optionEls.some(el => /selected|checked|active|current/i.test(el.className || ''));
    }

    // ---------------- 主流程 ----------------

    let running = false;

    async function runAll() {
        if (!cfg.base_url || !cfg.api_key || !cfg.model) {
            alert('请先通过 Tampermonkey 菜单设置 API(Base URL / Key / 模型)');
            promptApiCfg();
            return;
        }
        running = true;
        setPanelState(true);
        log(`开始,模型: ${cfg.model}`);

        try {
            let round = 0;
            while (running && round < 10) {
                // 每轮重新查询(点击后 DOM 可能重渲染)
                const containers = [...document.querySelectorAll(Q_CONTAINER_SEL)]
                    .filter(el => (el.innerText || '').trim().length > 10);
                if (!containers.length) { log('未找到题目容器,请确认当前是做题页'); break; }

                let did = 0, skip = 0, fail = 0;
                for (const c of containers) {
                    if (!running) break;
                    if (c.dataset.fxDone === '1') { skip++; continue; }

                    const q = extractQuestion(c);
                    if (!q.optionEls.length) { log(`跳过(未识别选项): ${q.stem.slice(0, 30)}`); c.dataset.fxDone = '2'; skip++; continue; }

                    try {
                        log(`作答: ${q.stem.slice(0, 40)}`);
                        const answer = await llmSolve(q);
                        log(`  答案: ${JSON.stringify(answer)}`);
                        for (const lb of answer) {
                            const el = findOptionEl(q, lb);
                            if (el) {
                                await sleep(rand(...cfg.opt_delay) * 1000);
                                clickEl(el);
                            } else log(`  选项 ${lb} 未找到对应元素`);
                        }
                        c.dataset.fxDone = '1';
                        did++;
                    } catch (e) {
                        log(`  失败: ${e.message}`);
                        c.dataset.fxDone = '2';
                        fail++;
                    }
                    await sleep(rand(...cfg.q_delay) * 1000);
                }

                log(`第${++round}轮完成:作答 ${did} / 跳过 ${skip} / 失败 ${fail}`);
                // 全部处理完且无失败 → 结束;有失败(网络等)再扫一轮
                if (did === 0 && fail === 0) break;
                if (fail === 0 && skip === 0) break;
                await sleep(2000);
            }
        } finally {
            running = false;
            setPanelState(false);
            log('结束');
        }
    }

    // ---------------- 悬浮面板 ----------------

    let panel, logBox;

    function buildPanel() {
        panel = document.createElement('div');
        panel.style.cssText = [
            'position:fixed', 'right:16px', 'bottom:16px', 'z-index:2147483647',
            'width:300px', 'max-height:260px', 'display:flex', 'flex-direction:column',
            'background:#1e2430', 'color:#dfe6f0', 'border-radius:10px',
            'font:12px/1.5 system-ui,sans-serif', 'box-shadow:0 4px 16px rgba(0,0,0,.4)',
            'overflow:hidden',
        ].join(';');

        const bar = document.createElement('div');
        bar.style.cssText = 'display:flex;gap:8px;align-items:center;padding:8px 10px;background:#2a3244;cursor:move;user-select:none';
        bar.innerHTML = '<b style="flex:1">fuck_DXSTJ 网页版</b>';

        const btnRun = document.createElement('button');
        btnRun.textContent = '开始';
        btnRun.style.cssText = 'padding:3px 12px;border:0;border-radius:6px;background:#3b82f6;color:#fff;cursor:pointer';
        btnRun.onclick = () => { if (!running) runAll(); else { running = false; btnRun.textContent = '开始'; } };

        const btnHide = document.createElement('button');
        btnHide.textContent = '—';
        btnHide.style.cssText = 'padding:3px 8px;border:0;border-radius:6px;background:#475069;color:#fff;cursor:pointer';
        let collapsed = false;
        btnHide.onclick = () => {
            collapsed = !collapsed;
            logBox.style.display = collapsed ? 'none' : 'block';
        };

        bar.appendChild(btnRun);
        bar.appendChild(btnHide);

        logBox = document.createElement('div');
        logBox.style.cssText = 'padding:8px 10px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;max-height:200px';

        panel.appendChild(bar);
        panel.appendChild(logBox);
        document.body.appendChild(panel);

        // 拖动
        let dragging = null;
        bar.addEventListener('mousedown', e => {
            if (e.target.tagName === 'BUTTON') return;
            dragging = { x: e.clientX - panel.offsetLeft, y: e.clientY - panel.offsetTop };
            e.preventDefault();
        });
        window.addEventListener('mousemove', e => {
            if (!dragging) return;
            panel.style.left = Math.max(0, e.clientX - dragging.x) + 'px';
            panel.style.top = Math.max(0, e.clientY - dragging.y) + 'px';
            panel.style.right = 'auto'; panel.style.bottom = 'auto';
        });
        window.addEventListener('mouseup', () => dragging = null);
    }

    function setPanelState(run) {
        const btn = panel.querySelector('button');
        if (btn) btn.textContent = run ? '停止' : '开始';
    }

    function log(msg) {
        const t = new Date().toTimeString().slice(0, 8);
        logBox.textContent += `[${t}] ${msg}\n`;
        logBox.scrollTop = logBox.scrollHeight;
        console.log('[fuck_DXSTJ]', msg);
    }

    if (window === window.top || document.querySelector(Q_CONTAINER_SEL)) {
        buildPanel();
        log('面板已加载。先在 Tampermonkey 菜单配置 API,再点"开始"。');
    }
})();
