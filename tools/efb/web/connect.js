// "Connect this iPad" (QR code 2 on the PC's setup page opens http://<pc>:47823/connect#k=<KEY>). The key stays in the
// fragment, so it never travels in a request. Connect opens the installed "Send to SR20" Shortcut with the send address
// as its text input; the Shortcut saves it. Copy is the fallback for pasting it in by hand.
(function () {
    'use strict';
    const m = /[#&]k=([A-Za-z0-9]{8})/.exec(location.hash);
    const state = document.getElementById('state');
    if (!m) {
        state.textContent = 'This link is incomplete. Scan code 2 on the PC’s SR20 iPad setup page again.';
        document.getElementById('go').hidden = true;
        document.getElementById('copy').hidden = true;
        return;
    }
    const addr = location.origin + '/' + m[1].toUpperCase();
    document.getElementById('addr').textContent = addr;
    document.getElementById('go').href = 'shortcuts://run-shortcut?name=' + encodeURIComponent('Send to SR20') +
        '&input=text&text=' + encodeURIComponent(addr);
    document.getElementById('copy').addEventListener('click', async () => {
        const said = document.getElementById('copied');
        try {
            await navigator.clipboard.writeText(addr);
            said.textContent = 'Copied.';
        } catch (e) {
            said.textContent = 'Press and hold the address above to copy it.';
        }
    });
})();
