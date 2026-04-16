const applyInspector = (card) => {
    const image = document.querySelector("[data-inspector-image]");
    const name = document.querySelector("[data-inspector-name]");
    const size = document.querySelector("[data-inspector-size]");
    const extra = document.querySelector("[data-inspector-extra]");

    if (!image || !name || !size || !extra) {
        return;
    }

    image.src = card.dataset.preview;
    name.textContent = card.dataset.name;
    size.textContent = card.dataset.size;
    extra.textContent = `${card.dataset.ext} • ${card.dataset.uploaded}`;
};

document.addEventListener("DOMContentLoaded", () => {
    const cards = document.querySelectorAll("[data-file-card]");
    cards.forEach((card) => {
        card.addEventListener("click", (event) => {
            if (event.target.closest("a, button, form, input, label")) {
                return;
            }
            applyInspector(card);
        });
    });
});
