document.addEventListener('DOMContentLoaded', () => {
    const siteSelect = document.getElementById('site-select');
    const categorySelect = document.getElementById('category-select');
    const keywordsInput = document.getElementById('keywords-input');
    const startBtn = document.getElementById('start-btn');
    const terminal = document.getElementById('terminal');
    const statusBadge = document.getElementById('status-badge');
    const downloadArea = document.getElementById('download-area');
    const downloadBtn = document.getElementById('download-btn');
    
    let serverKeywords = {};
    const credPanel = document.getElementById('ungm-credentials');

    // Fetch config
    fetch('/config')
        .then(res => res.json())
        .then(data => {
            // Populate sites with friendly names
            siteSelect.innerHTML = data.sites.map(s => {
                const label = s === 'ungm' ? 'UNGM (UN Global Marketplace)' : s.toUpperCase();
                return `<option value="${s}">${label}</option>`;
            }).join('');

            // Show/hide credential panel on initial load
            toggleCredPanel();

            // Populate categories
            serverKeywords = data.keywords;
            categorySelect.innerHTML = '<option value="custom">Custom (Type below)</option>';
            categorySelect.innerHTML += Object.keys(serverKeywords).map(k => `<option value="${k}">${k}</option>`).join('');

            if (Object.keys(serverKeywords).length > 0) {
                categorySelect.value = Object.keys(serverKeywords)[0];
                updateKeywordsFromCategory();
            }
        });

    // Show credential panel only when UNGM is selected
    siteSelect.addEventListener('change', toggleCredPanel);

    function toggleCredPanel() {
        if (siteSelect.value === 'ungm') {
            credPanel.classList.remove('hidden');
        } else {
            credPanel.classList.add('hidden');
        }
    }

    categorySelect.addEventListener('change', updateKeywordsFromCategory);

    function updateKeywordsFromCategory() {
        const cat = categorySelect.value;
        if (cat === 'custom') {
            keywordsInput.value = '';
        } else if (serverKeywords[cat]) {
            keywordsInput.value = serverKeywords[cat].join(', ');
        }
    }

    function appendLog(msg, type='normal') {
        const div = document.createElement('div');
        div.className = `log-line ${type}`;
        
        // Colorize based on content emojis if not explicitly typed
        if (type === 'normal') {
            if (msg.includes('❌') || msg.includes('⚠️')) type = 'error';
            else if (msg.includes('✅') || msg.includes('💾')) type = 'success';
            else if (msg.includes('🔍') || msg.includes('▶️')) type = 'info';
            else if (msg.includes('🤖')) type = 'system';
            div.className = `log-line ${type}`;
        }
        
        const now = new Date();
        const timeStr = now.toLocaleTimeString([], {hour12: false, hour: '2-digit', minute:'2-digit', second:'2-digit'});
        
        let prefix = '[LOG]';
        if (type === 'error') prefix = '[ERROR]';
        else if (type === 'success') prefix = '[SUCCESS]';
        else if (type === 'info') prefix = '[INFO]';
        else if (type === 'system') prefix = '[SYSTEM]';
        
        const timeSpan = document.createElement('span');
        timeSpan.className = 'timestamp';
        timeSpan.textContent = `[${timeStr}]`;
        
        const prefixSpan = document.createElement('span');
        prefixSpan.className = 'prefix';
        prefixSpan.textContent = prefix;
        
        const textSpan = document.createElement('span');
        textSpan.textContent = ` ${msg}`;
        
        div.appendChild(timeSpan);
        div.appendChild(prefixSpan);
        div.appendChild(textSpan);
        
        terminal.appendChild(div);
        terminal.scrollTop = terminal.scrollHeight;
    }

    startBtn.addEventListener('click', () => {
        const site = siteSelect.value;
        const kwString = keywordsInput.value;
        const keywords = kwString.split(',').map(k => k.trim()).filter(k => k.length > 0);

        if (!site || keywords.length === 0) {
            alert('Please select a site and enter at least one keyword.');
            return;
        }

        // Validate UNGM credentials if needed
        let credentials = {};
        if (site === 'ungm') {
            const email = document.getElementById('ungm-email').value.trim();
            const password = document.getElementById('ungm-password').value;
            if (!email || !password) {
                alert('Please enter your UNGM email and password.');
                return;
            }
            const showBrowser = document.getElementById('ungm-show-browser').checked;
            credentials = { email, password, show_browser: showBrowser };
        }

        // UI Updates
        startBtn.disabled = true;
        startBtn.querySelector('.btn-text').textContent = 'Scraping...';
        startBtn.querySelector('.loader').classList.remove('hidden');
        downloadArea.classList.add('hidden');
        statusBadge.className = 'badge running';
        statusBadge.textContent = 'Running';
        terminal.innerHTML = '';

        appendLog('Connecting to backend...', 'system');

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/scrape`);

        ws.onopen = () => {
            appendLog('Connected. Initializing scraper...', 'success');
            const payload = { site, keywords };
            if (site === 'ungm') payload.credentials = credentials;
            ws.send(JSON.stringify(payload));
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === 'log') {
                appendLog(data.message);
            } else if (data.type === 'complete') {
                statusBadge.className = 'badge completed';
                statusBadge.textContent = 'Completed';
                downloadArea.classList.remove('hidden');
                downloadBtn.href = `/download?name=${encodeURIComponent(data.zip)}`;
                downloadBtn.textContent = '📥 Download All Results (ZIP)';
                finishRun();
            } else if (data.type === 'error') {
                statusBadge.className = 'badge failed';
                statusBadge.textContent = 'Failed';
                appendLog(data.message, 'error');
                finishRun();
            }
        };

        ws.onerror = (error) => {
            appendLog('WebSocket error occurred.', 'error');
            statusBadge.className = 'badge failed';
            statusBadge.textContent = 'Disconnected';
            finishRun();
        };

        ws.onclose = () => {
            if (statusBadge.textContent === 'Running') {
                appendLog('Connection closed unexpectedly.', 'error');
                statusBadge.className = 'badge failed';
                statusBadge.textContent = 'Disconnected';
                finishRun();
            }
        };

        function finishRun() {
            startBtn.disabled = false;
            startBtn.querySelector('.btn-text').textContent = 'Start Scraping';
            startBtn.querySelector('.loader').classList.add('hidden');
        }
    });
});
