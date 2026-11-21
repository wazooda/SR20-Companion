// Fallback upload page (efb_lan.py): pairs with the #p= code from the setup QR, keeps the send address it gets back,
// and posts the chosen file raw to it.
(function () {
    'use strict';
    const KEY = 'sr20-send-url';
    const state = document.getElementById('state'), out = document.getElementById('out');
    const send = document.getElementById('send'), file = document.getElementById('file');
    function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
    function store(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* private browsing: this visit only */ } }
    let url = stored();
    async function pair(code) {
        const r = await fetch('/v1/pair?p=' + encodeURIComponent(code), { method: 'POST' });
        const t = await r.text();
        if (!r.ok) { throw new Error(t); }
        url = t.trim();
        store(url);
    }
    async function init() {
        const m = /[#&]p=([A-Za-z0-9]+)/.exec(location.hash);
        if (m) {
            history.replaceState(null, '', location.pathname);
            try { await pair(m[1]); } catch (e) { state.textContent = e.message; return; }
        }
        if (!url) {
            state.textContent = 'Not set up yet: on the PC, open SR20 iPad setup and point this iPad’s camera at the code.';
            return;
        }
        state.textContent = 'Ready.';
        send.disabled = false;
    }
    send.addEventListener('click', async () => {
        const f = file.files[0];
        if (!f) { out.textContent = 'Choose the flight plan file first.'; return; }
        send.disabled = true;
        out.textContent = 'Sending…';
        try {
            const sendUrl = new URL(url);
            if (sendUrl.origin !== location.origin) { throw new Error('open this page from the code on the PC’s setup page again.'); }
            const r = await fetch(sendUrl.pathname + sendUrl.search, { method: 'POST', body: f });
            out.textContent = await r.text();
        } catch (e) {
            out.textContent = 'Not sent: ' + e.message;
        }
        send.disabled = false;
    });
    init();
})();
