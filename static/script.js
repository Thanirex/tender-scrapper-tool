document.addEventListener('DOMContentLoaded', () => {
    // Step elements
    const siteSelect       = document.getElementById('site-select');
    const categorySelect   = document.getElementById('category-select');
    const keywordsInput    = document.getElementById('keywords-input');
    const ungmEmail        = document.getElementById('ungm-email');
    const ungmPassword     = document.getElementById('ungm-password');
    const browserToggleRow = document.getElementById('browser-toggle-row');
    const ungmShowBrowser  = document.getElementById('ungm-show-browser');
    const btnStart         = document.getElementById('btn-start');
    // legacy refs kept null-safe (removed from HTML)
    const btnNext = null;
    const btnPrev = null;

    // State panels
    const stateIdle    = document.getElementById('state-idle');
    const stateRunning = document.getElementById('state-running');
    const stateDone    = document.getElementById('state-done');
    const statusBadge  = document.getElementById('status-badge');

    // Results section (full-width below panels)
    const resultsSectionEl = document.getElementById('results-section');
    const resultsGrid      = document.getElementById('results-grid');
    const resultsCount     = document.getElementById('results-count');
    const resultsSummary   = document.getElementById('results-summary');
    const downloadBtn      = document.getElementById('download-btn');

    // Notification bar
    const notifBar       = document.getElementById('notif-bar');
    const notifKeywordEl = document.getElementById('notif-keyword');
    const notifCountEl   = document.getElementById('notif-count-val');

    // Thought stream
    const thoughtStream = document.getElementById('thought-stream');

    // Logs Slider
    const logsBar = document.getElementById('logs-bar');
    const logsHeader = document.getElementById('logs-header');
    const logsContent = document.getElementById('logs-content');

    if (logsHeader) {
        logsHeader.addEventListener('click', () => {
            logsBar.classList.toggle('expanded');
        });
    }

    function appendLog(rawMsg) {
        if (!logsContent) return;
        const line = document.createElement('div');
        line.className = 'log-line';
        line.textContent = rawMsg;
        logsContent.appendChild(line);
        logsContent.scrollTop = logsContent.scrollHeight;
    }

    let serverKeywords  = {};
    let foundCount      = 0;
    let allResults      = [];
    let currentKeyword  = '';
    let keywordCountMap = {};
    let stepGoneTimers  = {};
    let thoughtTimer    = null;

    // ── Step 2 lock/unlock based on selected site ─────────────────────────

    const step2Card      = document.getElementById('step-card-2');
    const step2NoAuth    = document.getElementById('step2-no-auth');
    const step2AuthFields = document.getElementById('step2-auth-fields');
    const step2SiteName  = document.getElementById('step2-site-name');
    const step2LockIcon  = document.getElementById('step2-lock-icon');
    const step2Num       = document.getElementById('step2-num');

    const SITES_REQUIRING_AUTH = new Set(['ungm']);

    function updateStep2(site) {
        const needsAuth = SITES_REQUIRING_AUTH.has(site);
        const siteLabel = site
            ? (site === 'ungm' ? 'UNGM (UN Global Marketplace)' : site.toUpperCase())
            : '—';

        if (needsAuth) {
            step2Card.classList.remove('step-card-locked');
            step2NoAuth.classList.add('hidden');
            step2AuthFields.classList.remove('hidden');
            if (step2LockIcon) step2LockIcon.textContent = '🔓';
            if (step2Num) step2Num.classList.remove('step-num-locked');
        } else {
            step2Card.classList.add('step-card-locked');
            step2AuthFields.classList.add('hidden');
            step2NoAuth.classList.remove('hidden');
            if (step2SiteName) step2SiteName.textContent = siteLabel;
            if (step2LockIcon) step2LockIcon.textContent = '🔒';
            if (step2Num) step2Num.classList.add('step-num-locked');
        }

        if (browserToggleRow) {
            browserToggleRow.classList.toggle('hidden', site !== 'ungm');
        }
    }

    siteSelect.addEventListener('change', () => updateStep2(siteSelect.value));

    fetch('/config')
        .then(res => res.json())
        .then(data => {
            siteSelect.innerHTML = data.sites.map(s => {
                const label = s === 'ungm' ? 'UNGM (UN Global Marketplace)' : s === 'au' ? 'African Union Bids' : s === 'acbf' ? 'ACBF Procurement & Consultancies' : s === 'trademarkafrica' ? 'TradeMark Africa Procurement' : s.toUpperCase();
                return `<option value="${s}">${label}</option>`;
            }).join('');
            serverKeywords = data.keywords;
            categorySelect.innerHTML = '<option value="custom">Custom (Type above)</option>';
            categorySelect.innerHTML += Object.keys(serverKeywords)
                .map(k => `<option value="${k}">${k}</option>`).join('');
            siteSelect.value = data.sites[0];
            updateStep2(siteSelect.value);
        });

    categorySelect.addEventListener('change', () => {
        const cat = categorySelect.value;
        if (cat === 'custom') keywordsInput.value = '';
        else if (serverKeywords[cat]) keywordsInput.value = serverKeywords[cat].join(', ');
    });

    // ── Friendly message translation ──────────────────────────────────────

    function toFriendlyMsg(msg) {
        if (msg.includes('Processing keyword:') || msg.includes('Keyword:')) {
            const m = msg.match(/['"](.*?)['"]/);
            return m ? `Searching for "${m[1]}"…` : 'Starting keyword search…';
        }
        if (msg.match(/reports \d+ result/i)) {
            const m = msg.match(/(\d+)\s+results?/i);
            if (m) {
                const n = parseInt(m[1]);
                return n === 0 ? 'No results for this keyword' : `Found ${n} tender${n === 1 ? '' : 's'} to analyse`;
            }
        }
        if (msg.match(/Opening \d+ tenders/i)) {
            const m = msg.match(/Opening (\d+)/i);
            return m ? `TAiQ is opening ${m[1]} tender${m[1] === '1' ? '' : 's'}…` : 'Opening tenders…';
        }
        if (msg.match(/\[\d+\/\d+\]/)) {
            const m = msg.match(/\[(\d+)\/(\d+)\]/);
            return m ? `TAiQ is reading tender ${m[1]} of ${m[2]}…` : null;
        }
        if (msg.includes('Opening notice page') || msg.includes('Opening notice')) return 'TAiQ is reading the notice page…';
        if (msg.match(/\d+ verified fields scraped/)) {
            const m = msg.match(/(\d+) verified fields/);
            return `Collected ${m ? m[1] : ''} key details from this tender`;
        }
        if (msg.includes('No attachments')) return 'No file attachments for this tender';
        if (msg.toLowerCase().includes('downloading') || msg.includes('attachment')) return 'TAiQ has downloaded the tender files';
        if (msg.includes('Summarizing:')) {
            const m = msg.match(/Summarizing:\s*(.+)/);
            if (m) {
                let t = m[1].trim();
                if (t.length > 45) t = t.substring(0, 45) + '…';
                return `TAiQ is analysing "${t}"`;
            }
        }
        if (msg.includes('[Summarizer]') || msg.includes('Extracting Level')) return 'TAiQ is reading deep into the details…';
        if (msg.includes('Saved:')) return 'Opportunity summarised and saved ✓';
        if (msg.match(/tenders? processed for/i)) {
            const m = msg.match(/(\d+) tenders? processed for ['"](.+?)['"]/i);
            return m ? `"${m[2]}" complete — ${m[1]} result${m[1] === '1' ? '' : 's'}` : 'Keyword complete ✓';
        }
        if (msg.match(/Done\.\s+\d+ Level/)) return 'All tenders processed!';
        if (msg.includes('Packaging all results')) return 'TAiQ is packaging your results…';
        if (msg.includes('Opening UNGM login') || msg.includes('Opening login')) return 'Opening the login page…';
        if (msg.includes('Logged in') || msg.match(/✅ Logged in/)) return 'Logged in successfully ✓';
        if (msg.includes('Active-only filter')) return 'Active tenders filter applied';
        if (msg.includes('Search submitted')) {
            const m = msg.match(/for ['"](.+?)['"]/);
            return m ? `Searching "${m[1]}"…` : 'Search submitted…';
        }
        return null;
    }

    // ── Progress tracker ──────────────────────────────────────────────────

    let currentTrackerStep = 'connecting';

    function setTrackerStep(stepId, friendlyMsg) {
        currentTrackerStep = stepId;
        const steps = ['connecting', 'login', 'searching', 'extracting'];
        const stepIndex = steps.indexOf(stepId);

        steps.forEach((id, idx) => {
            const el = document.getElementById(`step-${id}`);
            if (!el || el.classList.contains('step-gone')) return;
            el.classList.remove('active', 'done');

            if (id === stepId) {
                el.classList.add('active');
                if (id === 'searching') {
                    document.getElementById('scan-dots')?.classList.remove('hidden');
                }
                if (id === 'extracting') {
                    // Robot face only shown explicitly when summarizing begins — not here
                    document.getElementById('scan-dots')?.classList.add('hidden');
                    document.getElementById('keyword-pill')?.classList.add('hidden');
                }
            } else if (idx < stepIndex) {
                el.classList.add('done');
                // Connecting and login fade away after being marked done
                if ((id === 'connecting' || id === 'login') && !stepGoneTimers[id]) {
                    stepGoneTimers[id] = setTimeout(() => {
                        el.classList.add('step-gone');
                    }, 1800);
                }
            }
        });

        if (friendlyMsg) pushThought(friendlyMsg);
    }

    // ── Keyword pill ──────────────────────────────────────────────────────

    function setKeywordPill(keyword) {
        const pill = document.getElementById('keyword-pill');
        const pillText = document.getElementById('keyword-pill-text');
        if (!pill || !pillText) return;
        pillText.textContent = `"${keyword}"`;
        pill.classList.remove('hidden', 'pill-enter');
        void pill.offsetWidth; // reflow to restart animation
        pill.classList.add('pill-enter');
    }

    // ── Notification bar ──────────────────────────────────────────────────

    function showNotif(keyword, count) {
        if (!notifBar) return;
        if (notifKeywordEl) notifKeywordEl.textContent = `"${keyword}"`;
        if (notifCountEl) notifCountEl.textContent = count;
        notifBar.classList.remove('hidden');
        notifBar.style.animation = 'none';
        void notifBar.offsetWidth;
        notifBar.style.animation = '';
    }

    function hideNotif() {
        if (notifBar) notifBar.classList.add('hidden');
    }

    // ── Thought bubbles ──────────────────────────────────────────────────

    function pushThought(text) {
        if (!thoughtStream || !text) return;

        const existing = thoughtStream.querySelectorAll('.thought-bubble:not(.fading)');
        existing.forEach(b => {
            b.classList.add('fading');
            setTimeout(() => b.remove(), 360);
        });

        const bubble = document.createElement('div');
        bubble.className = 'thought-bubble';
        bubble.textContent = text;
        thoughtStream.appendChild(bubble);

        if (thoughtTimer) clearTimeout(thoughtTimer);
        thoughtTimer = setTimeout(() => {
            if (bubble.parentNode) {
                bubble.classList.add('fading');
                setTimeout(() => bubble.remove(), 360);
            }
        }, 5500);
    }

    // ── Card rendering ────────────────────────────────────────────────────

    function createCardInnerHTML(record) {
        let html = `<h4>${record.title || 'Unknown Opportunity'}</h4>`;
        html += `<div class="card-fields">`;
        if (record.fields) {
            let shown = 0;
            for (const [k, v] of Object.entries(record.fields)) {
                if (v && String(v).length > 0 && shown < 3) {
                    let val = String(v);
                    if (val.length > 55) val = val.substring(0, 55) + '…';
                    html += `<div class="card-field"><strong>${k}</strong><span>${val}</span></div>`;
                    shown++;
                }
            }
        }
        html += `</div>`;
        html += `<div class="card-actions">`;
        if (record.url) html += `<a href="${record.url}" target="_blank" class="card-link">View ↗</a>`;
        if (record.tender_dir) {
            const token = getToken() || '';
            const dlUrl = `/download/tender?path=${encodeURIComponent(record.tender_dir)}&token=${encodeURIComponent(token)}`;
            html += `<a href="${dlUrl}" class="card-dl-btn" download>
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                        Download All
                     </a>`;
            html += `<button class="card-files-btn" data-dir="${record.tender_dir}">📎 Files</button>`;
        }
        html += `</div>`;
        return html;
    }

    async function _toggleCardFiles(btn) {
        const card = btn.closest('.result-card');
        let list = card.querySelector('.card-files-list');
        if (list) {
            const hidden = list.style.display === 'none';
            list.style.display = hidden ? '' : 'none';
            btn.textContent = hidden ? '📎 Hide files' : '📎 Files';
            return;
        }
        btn.textContent = 'Loading…';
        btn.disabled = true;
        try {
            const token = getToken() || '';
            const dir = btn.dataset.dir;
            const res = await fetch(`/tender/files?dir=${encodeURIComponent(dir)}&token=${encodeURIComponent(token)}`);
            const files = await res.json();
            if (!Array.isArray(files) || files.length === 0) {
                btn.textContent = '📎 No files';
                return;
            }
            const token2 = getToken() || '';
            list = document.createElement('div');
            list.className = 'card-files-list';
            list.innerHTML = files.map(f => {
                const url = `/download/file?path=${encodeURIComponent(f.path)}&token=${encodeURIComponent(token2)}`;
                return `<div class="card-file-item">
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    <a href="${url}" download title="${f.name}">${f.name}</a>
                </div>`;
            }).join('');
            card.appendChild(list);
            btn.textContent = '📎 Hide files';
        } catch (_) {
            btn.textContent = '📎 Error';
        } finally {
            btn.disabled = false;
        }
    }

    if (resultsGrid) {
        resultsGrid.addEventListener('click', e => {
            const btn = e.target.closest('.card-files-btn');
            if (btn) _toggleCardFiles(btn);
        });
    }

    // ── Start Extraction ──────────────────────────────────────────────────

    btnStart.addEventListener('click', () => {
        const site = siteSelect.value;
        const keywords = keywordsInput.value.split(',').map(k => k.trim()).filter(k => k.length > 0);

        if (!site || keywords.length === 0) {
            alert('Please select a site and enter at least one keyword.');
            return;
        }

        let credentials = {};
        if (site === 'ungm') {
            const email = ungmEmail.value.trim();
            const password = ungmPassword.value;
            if (!email || !password) {
                alert('Please enter your UNGM email and password in Step 2.');
                return;
            }
            credentials = { email, password, show_browser: ungmShowBrowser.checked };
        }

        // Reset run state
        foundCount      = 0;
        allResults      = [];
        currentKeyword  = '';
        keywordCountMap = {};
        stepGoneTimers  = {};
        if (thoughtStream) thoughtStream.innerHTML = '';
        if (logsContent) logsContent.innerHTML = '';

        // Reset tracker steps
        ['connecting', 'login', 'searching', 'extracting'].forEach(id => {
            const el = document.getElementById(`step-${id}`);
            if (el) el.classList.remove('done', 'active', 'step-gone');
        });
        document.getElementById('keyword-pill')?.classList.add('hidden');
        document.getElementById('scan-dots')?.classList.add('hidden');
        document.getElementById('robot-container')?.classList.add('hidden');

        // For non-UNGM sites the login step is irrelevant — hide it immediately
        if (site !== 'ungm') {
            document.getElementById('step-login')?.classList.add('step-gone');
        }

        // Hide results section until complete
        resultsSectionEl?.classList.add('hidden');
        if (resultsGrid) resultsGrid.innerHTML = '';

        // Switch to running state
        btnStart.disabled = true;
        btnStart.innerHTML = 'Starting… <span>⏳</span>';

        stateIdle.classList.remove('active');
        stateDone.classList.remove('active');
        stateRunning.classList.add('active');
        statusBadge.className = 'status-badge pulse';
        statusBadge.textContent = 'Running';
        setTrackerStep('connecting');
        pushThought('Establishing connection…');

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsToken = getToken() || '';
        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/scrape?token=${encodeURIComponent(wsToken)}`);

        ws.onopen = () => {
            setTrackerStep('connecting', 'Connected — sending request…');
            const payload = { site, keywords };
            if (site === 'ungm') payload.credentials = credentials;
            ws.send(JSON.stringify(payload));
        };

        ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);

            if (msg.type === 'ping') return; // server keepalive — ignore

            if (msg.type === 'log') {
                const raw = msg.message;
                const friendly = toFriendlyMsg(raw);
                appendLog(raw);

                if (raw.includes('Opening UNGM login') || raw.includes('Opening login') ||
                    raw.includes('Logged in') || raw.includes('Active-only filter')) {
                    setTrackerStep('login', friendly);

                } else if (raw.includes('Processing keyword:') || raw.includes('Keyword:') ||
                           raw.includes('Search submitted') || raw.match(/Found \d+ result/i)) {
                    // Extract and display keyword pill
                    const kwMatch = raw.match(/['"]([\w\s\-/]+?)['"]/);
                    if (kwMatch) {
                        currentKeyword = kwMatch[1];
                        if (!keywordCountMap[currentKeyword]) keywordCountMap[currentKeyword] = 0;
                        setKeywordPill(currentKeyword);
                        showNotif(currentKeyword, keywordCountMap[currentKeyword]);
                    }
                    setTrackerStep('searching', friendly);

                } else if (raw.match(/reports \d+ result/i)) {
                    const m = raw.match(/(\d+)\s+results?/i);
                    if (m && currentKeyword) {
                        keywordCountMap[currentKeyword] = parseInt(m[1]);
                        showNotif(currentKeyword, parseInt(m[1]));
                    }
                    setTrackerStep('searching', friendly);

                } else if (raw.match(/\[\d+\/\d+\]/) || raw.includes('Opening notice') ||
                           raw.includes('verified fields') || raw.includes('Summarizing:') ||
                           raw.includes('[Summarizer]') || raw.includes('Extracting Level') ||
                           raw.includes('Saved:') || raw.includes('No attachments') ||
                           raw.includes('Packaging all results') || raw.match(/Done\.\s+\d+/)) {
                    setTrackerStep('extracting', friendly);
                    // Robot face appears only when TAiQ starts summarising
                    if (raw.includes('Summarizing:') || raw.includes('[Summarizer]') || raw.includes('Extracting Level')) {
                        document.getElementById('robot-container')?.classList.remove('hidden');
                    }

                } else if (friendly) {
                    pushThought(friendly);
                }

            } else if (msg.type === 'result') {
                allResults.push(msg.data);
                foundCount++;

            } else if (msg.type === 'complete') {
                finishRun(msg.zip);

            } else if (msg.type === 'error') {
                statusBadge.className = 'status-badge';
                statusBadge.style.background = 'var(--danger)';
                statusBadge.style.color = 'white';
                statusBadge.textContent = 'Failed';
                pushThought('Something went wrong — please try again.');
                setTimeout(() => alert(`Error: ${msg.message}`), 100);
                resetStartBtn();
            }
        };

        ws.onerror = () => { alert('Connection lost.'); resetStartBtn(); };
        ws.onclose = () => {
            if (statusBadge.textContent === 'Running') {
                alert('Connection closed unexpectedly.');
                resetStartBtn();
            }
        };

        function finishRun(zipFilename) {
            hideNotif();

            stateRunning.classList.remove('active');
            stateDone.classList.add('active');

            // Re-trigger checkmark animation (in case this is a second run)
            const circle = stateDone.querySelector('.done-circle');
            const checkPath = stateDone.querySelector('.done-check');
            [[circle, 'circleIn 0.6s cubic-bezier(0.65,0,0.45,1) 0.15s forwards'],
             [checkPath, 'checkIn 0.38s cubic-bezier(0.65,0,0.45,1) 0.65s forwards']].forEach(([el, anim]) => {
                if (!el) return;
                el.style.animation = 'none';
                void el.offsetWidth;
                el.style.animation = anim;
            });

            const doneSummary = document.getElementById('done-summary');
            if (doneSummary) {
                doneSummary.textContent = foundCount > 0
                    ? `TAiQ found ${foundCount} opportunit${foundCount === 1 ? 'y' : 'ies'} across your keywords.`
                    : 'No opportunities were found for these keywords.';
            }

            // Populate and reveal the full-width results section
            if (resultsSectionEl) {
                if (resultsCount) resultsCount.textContent = `${foundCount} Found`;
                if (resultsSummary) {
                    resultsSummary.textContent = foundCount > 0
                        ? `TAiQ found and summarised ${foundCount} tender opportunit${foundCount === 1 ? 'y' : 'ies'} across your keywords.`
                        : 'No opportunities were found for these keywords.';
                }
                if (downloadBtn) {
                    if (zipFilename) {
                        downloadBtn.href = `/download?name=${encodeURIComponent(zipFilename)}`;
                        downloadBtn.style.display = '';
                    } else {
                        downloadBtn.style.display = 'none';
                    }
                }

                if (allResults.length > 0) {
                    resultsGrid.innerHTML = allResults.map((r, i) =>
                        `<div class="result-card" style="animation-delay:${Math.min(i * 0.05, 0.6)}s">${createCardInnerHTML(r)}</div>`
                    ).join('');
                } else {
                    resultsGrid.innerHTML = `<p class="no-results">No opportunities found for these keywords.</p>`;
                }

                resultsSectionEl.classList.remove('hidden');
                setTimeout(() => {
                    resultsSectionEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }, 100);
            }

            resetStartBtn();
            btnStart.innerHTML = 'Search Again <span>↻</span>';
        }

        function resetStartBtn() {
            btnStart.disabled = false;
            btnStart.innerHTML = 'Start Extraction <span>🚀</span>';
        }
    });
});
