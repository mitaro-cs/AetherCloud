let deferredPrompt;

const installSelectors = () => {
    const buttons = document.querySelectorAll("[data-install-trigger]");
    buttons.forEach((button) => {
        button.hidden = !deferredPrompt;
        button.addEventListener("click", async () => {
            if (!deferredPrompt) {
                return;
            }
            deferredPrompt.prompt();
            await deferredPrompt.userChoice;
            deferredPrompt = null;
            buttons.forEach((item) => {
                item.hidden = true;
            });
        });
    });
};

window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    installSelectors();
});

window.addEventListener("appinstalled", () => {
    deferredPrompt = null;
    document.querySelectorAll("[data-install-trigger]").forEach((button) => {
        button.hidden = true;
    });
});

window.addEventListener("load", () => {
    if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
    installSelectors();
});
