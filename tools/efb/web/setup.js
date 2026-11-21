// SR20 iPad setup page (efb_setup.py): buttons are same-origin POSTs with the page's nonce; the firewall is read async.
(function () {
    'use strict';
    const nonce = document.querySelector('meta[name="sr20-nonce"]').content;
    const fw = document.getElementById('fw');
    const fix = document.getElementById('fix');
    async function act(name, btn) {
        btn.disabled = true;
        const old = btn.textContent;
        if (name === 'fix-firewall') { btn.textContent = 'Answer the Windows prompt on this PC…'; }
        try {
            const r = await fetch('/setup/' + name, { method: 'POST', body: new URLSearchParams({ nonce }),
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
            const d = await r.json();
            if (name !== 'fix-firewall') {
                location.reload();
                return;
            }
            const said = { done: 'Done: the SR20 Companion may now receive from your home network.',
                failed: 'Windows refused one of the rules. Try again, or allow it in Windows Security.',
                cancelled: 'Not changed (the Windows prompt was cancelled).' }[d.result] || ('Not changed: ' + d.result);
            fw.textContent = said;
            setTimeout(loadFw, 1500);
        } catch (e) {
            btn.textContent = 'Failed: ' + e;
            return;
        }
        btn.textContent = old;
        btn.disabled = false;
    }
    function confirmForget(b) {   // two clicks instead of a browser dialog
        if (b.dataset.armed) { return true; }
        b.dataset.armed = '1';
        b.textContent = 'Click again to disconnect all iPads';
        setTimeout(() => { delete b.dataset.armed; b.textContent = 'Disconnect all iPads'; }, 4000);
        return false;
    }
    document.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', () => {
        if (b.dataset.act === 'forget' && !confirmForget(b)) { return; }
        act(b.dataset.act, b);
    }));
    async function loadFw() {
        fw.textContent = 'Checking the firewall…';
        try {
            const d = await (await fetch('/setup/firewall')).json();
            if (!d.ok) { fw.textContent = (d.why || 'Couldn’t check the firewall.') + ' If your iPad can’t connect, use the button below.'; fix.hidden = false; return; }
            const net = d.network ? ('this network (“' + d.network + '”)') : 'this network';
            const say = {
                public: 'Windows treats ' + net + ' as a Public network, which blocks the iPad. To fix it: Windows Settings > Network & internet > open this network > choose Private network.',
                blocked: 'A Windows Firewall rule is blocking the SR20 Companion. Use the button below to allow it on your home network only.',
                domain: 'This PC is on a work (Domain) network. Ask whoever manages it to allow the SR20 Companion.',
                'no-rule': 'Windows hasn’t been told to allow the SR20 Companion yet. If your iPad can’t connect, use the button below.',
                allowed: 'OK: Windows lets the SR20 Companion receive on your home network.' }[d.verdict] || '';
            fw.textContent = say + (d.frozen ? '' : ' (Developer copy: the rules are for ' + d.program + '.)');
            fix.hidden = !(d.verdict === 'blocked' || d.verdict === 'no-rule');
        } catch (e) {
            fw.textContent = 'Could not read the firewall.';
        }
    }
    loadFw();
})();
