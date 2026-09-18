/* Screen selection only. The API still verifies Telegram's signed initData. */
(function () {
    'use strict';
    const telegram = window.Telegram && window.Telegram.WebApp;
    const inTelegram = Boolean(telegram && typeof telegram.initData === 'string' && telegram.initData.trim());
    document.documentElement.classList.toggle('kepik-miniapp', inTelegram);
    if (inTelegram) {
        document.title = 'ККЭПик';
        document.getElementById('kepik-miniapp-style').media = 'all';
    }
}());
