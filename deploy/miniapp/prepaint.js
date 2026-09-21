/* Hide the public landing before first paint when Telegram launch data is present.
   These are the same hash/session sources used by the bundled Telegram SDK.
   This selects a screen only; API authentication is unchanged. */
(function () {
    'use strict';
    try {
        let hash = window.location.hash.replace(/^#/, '');
        const question = hash.indexOf('?');
        if (question >= 0) hash = hash.slice(question + 1);
        const params = new URLSearchParams(hash);
        let data;
        if (params.has('tgWebAppData')) {
            data = params.getAll('tgWebAppData').pop();
        } else {
            const cached = JSON.parse(window.sessionStorage.getItem('__telegram__initParams') || 'null');
            data = cached && cached.tgWebAppData;
        }
        if (typeof data === 'string' && data.trim()) {
            document.documentElement.classList.add('kepik-telegram-pending');
        }
    } catch (error) {
        // Blocked storage or malformed cache must not hide the ordinary website.
    }
}());
