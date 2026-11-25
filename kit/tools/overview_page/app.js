"use strict";

const byId = (id) => document.getElementById(id);
const listing = byId("listing");
const cardPanel = byId("card");
const rootsOnly = byId("open-roots");
const search = byId("search");
const statusFilter = byId("status-filter");
const parentFilter = byId("parent-filter");
const expanded = new Set();
const interval = Number(document.body.dataset.pollMs);
const run = location.pathname.split("/")[1];

let view = "tree";
let rows = null;
let byNumber = new Map();
let children = new Map();
let version = null;
let selected = null;
let detail = null;
let cardKey = "";
let loadFailed = false;
let request = null;
let timer = null;

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function taskLink(number, text, className) {
    const link = element("a", className, text);
    link.href = "#" + number;
    link.dataset.focus = (className || "link") + ":" + number;
    return link;
}

function badge(row) {
    const status = element("span", "status", row.status);
    status.dataset.kind = row.kind;
    return status;
}

function relatedTask(link, row) {
    const related = element("span", "related-task");
    related.append(link, badge(row));
    return related;
}

function dependencyLinks(row, showStatus = false) {
    const links = element("div", "dependencies");
    if (row.dependencies.length) {
        links.append(element("span", "", "Зависит от"));
        for (const number of row.dependencies) {
            const supplier = byNumber.get(number);
            const link = taskLink(number, number, "dependency");
            link.title = supplier?.title || number;
            link.dataset.focus += ":" + row.id;
            links.append(showStatus && supplier ? relatedTask(link, supplier) : link);
        }
    }
    return links;
}

function taskRow(row, canExpand = false) {
    const item = element("li", "task-item");
    item.dataset.number = row.id;
    const line = element("div", "task-row");
    if (canExpand) {
        const isExpanded = expanded.has(row.id);
        const button = element("button", "disclosure", isExpanded ? "▾" : "▸");
        button.type = "button";
        button.dataset.focus = "expand:" + row.id;
        button.setAttribute("aria-expanded", isExpanded);
        button.setAttribute("aria-label", (isExpanded ? "Свернуть " : "Раскрыть ") + row.id);
        button.addEventListener("click", () => {
            if (expanded.has(row.id)) expanded.delete(row.id);
            else expanded.add(row.id);
            renderList();
        });
        line.append(button);
    }
    const body = element("div", "task-body");
    const link = taskLink(row.id, row.title, "task-link");
    link.prepend(element("span", "task-number", row.id));
    if (selected === row.id) link.setAttribute("aria-current", "true");
    body.append(link);
    const meta = element("div", "task-meta");
    meta.append(badge(row), dependencyLinks(row));
    body.append(meta);
    line.append(body);
    item.append(line);
    return item;
}

function taskTree(roots) {
    const list = element("ul", "task-list tree");
    const levels = [{list, items: roots}];
    while (levels.length) {
        const level = levels.pop();
        for (const row of level.items) {
            const descendants = children.get(row.id) || [];
            const item = taskRow(row, descendants.length > 0);
            level.list.append(item);
            if (expanded.has(row.id) && descendants.length) {
                const branch = element("ul", "task-list branch");
                item.append(branch);
                levels.push({list: branch, items: descendants});
            }
        }
    }
    return list;
}

function renderList() {
    if (rows === null) return;
    const focus = listing.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
    let visible, list;
    if (view === "tree") {
        visible = (children.get(null) || []).filter((row) =>
            !rootsOnly.checked || row.kind === "open" || row.kind === "waiting");
        list = taskTree(visible);
    } else {
        const query = search.value.trim().toLocaleLowerCase("ru");
        const status = statusFilter.value;
        const parent = parentFilter.value;
        visible = rows.filter((row) =>
            (!status || row.status === status) &&
            (!parent || (parent === "none" ? row.parent === null : row.parent === parent)) &&
            (row.id.toLocaleLowerCase("ru").includes(query) || row.title.toLocaleLowerCase("ru").includes(query))
        );
        list = element("ul", "task-list");
        for (const row of visible) list.append(taskRow(row));
    }
    listing.replaceChildren(visible.length ? list : element("p", "placeholder", "Нет задач с этими условиями."));
    listing.setAttribute("aria-busy", "false");
    byId("list-count").hidden = view === "tree";
    byId("list-count").textContent = "Найдено " + visible.length + " из " + rows.length;
    if (focus) listing.querySelector('[data-focus="' + CSS.escape(focus) + '"]')?.focus({preventScroll: true});
}

function options(select, available) {
    const value = select.value;
    const label = select.selectedOptions[0]?.textContent;
    if (value && !available.some(([key]) => key === value)) available.push([value, label]);
    select.replaceChildren(...available.map(([key, text]) => new Option(text, key)));
    select.value = value;
}

function updateRows(next) {
    rows = next;
    byNumber = new Map();
    children = new Map();
    for (const row of rows) {
        byNumber.set(row.id, row);
        if (!children.has(row.parent)) children.set(row.parent, []);
        children.get(row.parent).push(row);
    }
    const statuses = [...new Set(rows.map((row) => row.status))].sort((a, b) => a.localeCompare(b, "ru"));
    options(statusFilter, [["", "Любой"], ...statuses.map((status) => [status, status])]);
    const parents = rows.filter((row) => children.has(row.id)).map((row) => [row.id, row.id + " · " + row.title]);
    options(parentFilter, [["", "Любой"], ["none", "Без родителя"], ...parents]);
    renderList();
}

function section(label, content) {
    const block = element("section", "card-section");
    block.append(element("h3", "", label), content);
    return block;
}

function copyControl(row) {
    const label = "Копировать номер и название задачи";
    const control = element("div", "card-copy");
    const button = element("button", "copy-button");
    button.type = "button";
    button.dataset.focus = "copy";
    button.setAttribute("aria-label", label);
    const icon = element("span", "copy-icon");
    icon.setAttribute("aria-hidden", "true");
    button.append(icon);
    const hint = element("span", "copy-hint", label);
    hint.setAttribute("role", "status");
    let attempt = 0;
    function resetHint() {
        attempt += 1;
        hint.textContent = label;
    }
    button.addEventListener("pointerleave", resetHint);
    button.addEventListener("blur", resetHint);
    button.addEventListener("click", async () => {
        const current = ++attempt;
        let message = "Номер и название скопированы.";
        try {
            await navigator.clipboard.writeText("`" + row.id + " " + row.title + "`");
        } catch {
            message = "Не удалось скопировать номер и название задачи.";
        }
        if (current === attempt) hint.textContent = message;
    });
    control.append(button, hint);
    return control;
}

function renderCard() {
    const row = byNumber.get(selected);
    const descendants = children.get(selected) || [];
    const parent = byNumber.get(row?.parent);
    const suppliers = row?.dependencies.map((number) => byNumber.get(number));
    const nextKey = JSON.stringify([selected, row, detail, descendants, parent, suppliers, loadFailed]);
    if (nextKey === cardKey) return;
    cardKey = nextKey;
    if (!selected || !row || detail?.id !== selected) {
        let message = "Выберите задачу из списка.";
        if (selected) message = loadFailed ? "Карточка пока недоступна." : "Загрузка карточки…";
        cardPanel.replaceChildren(element("p", "placeholder", message));
        return;
    }

    const header = element("header", "card-header");
    const identifiers = element("div", "card-identifiers");
    identifiers.append(element("span", "task-number", row.id), badge(row));
    header.append(identifiers, copyControl(row), element("h2", "", row.title));
    if (parent) {
        const relation = element("p", "parent-link");
        const link = taskLink(parent.id, parent.id + " · " + parent.title);
        relation.append(element("span", "", "Родитель"), relatedTask(link, parent));
        header.append(relation);
    }
    header.append(dependencyLinks(row, true));
    const content = [header, section("Цель", element("p", "prose", detail.goal))];
    if (detail.closure) {
        const closure = section("Решение", element("p", "prose", detail.closure.reason));
        const replacement = detail.closure.replacement;
        if (replacement) closure.append(taskLink(replacement, "Задача-замена " + replacement));
        content.push(closure);
    }
    if (detail.waiting) {
        const waiting = element("dl", "waiting");
        for (const [label, key] of [["Осталось", "remaining"], ["Почему ждёт", "reason"], ["Следующий шаг", "next_step"]]) {
            waiting.append(element("dt", "", label), element("dd", "prose", detail.waiting[key]));
        }
        content.push(section("Что осталось сделать", waiting));
    }
    if (detail.criteria.length) {
        const criteria = element("ul", "criteria");
        for (const criterion of detail.criteria) {
            const item = element("li", "");
            item.append(element("span", "task-number", criterion.id), element("span", "prose", criterion.expectation));
            criteria.append(item);
        }
        content.push(section("Критерии приёмки", criteria));
    }
    if (detail.description) content.push(section("Описание", element("p", "prose", detail.description)));
    if (descendants.length) {
        const list = element("ul", "task-list children");
        for (const child of descendants) list.append(taskRow(child));
        content.push(section("Подзадачи", list));
    }
    const focus = cardPanel.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
    cardPanel.replaceChildren(...content);
    if (focus) cardPanel.querySelector('[data-focus="' + CSS.escape(focus) + '"]')?.focus({preventScroll: true});
}

function switchView(next) {
    view = next;
    byId("tree-view").setAttribute("aria-pressed", view === "tree");
    byId("all-view").setAttribute("aria-pressed", view === "all");
    byId("tree-filters").hidden = view !== "tree";
    byId("all-filters").hidden = view !== "all";
    listing.scrollTop = 0;
    renderList();
}

function readSelection() {
    const fragment = location.hash.slice(1);
    selected = /^(P|F-[a-z0-9]+(?:-[a-z0-9]+)*)\/T-[0-9]{3,}$/.test(fragment) ? fragment : null;
    detail = null;
    loadFailed = false;
    for (const link of listing.querySelectorAll(".task-link")) {
        if (link.hash.slice(1) === selected) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
    }
    cardPanel.scrollTop = 0;
    renderCard();
}

function freshness(state, message, issue = "", checkedAt = null) {
    byId("freshness").dataset.state = state;
    if (byId("connection").textContent !== message) byId("connection").textContent = message;
    byId("issue").textContent = issue;
    byId("issue").hidden = !issue;
    const time = byId("checked-at");
    if (checkedAt !== null) {
        time.dateTime = checkedAt;
        time.textContent = new Date(checkedAt).toLocaleTimeString("ru", {timeStyle: "medium"});
        time.title = "Последнее успешное чтение";
        time.hidden = false;
    }
}

async function refresh() {
    clearTimeout(timer);
    request?.abort();
    const active = new AbortController();
    request = active;
    const timeout = setTimeout(() => active.abort(), 5000);
    const url = new URL("data", location.href);
    if (selected) url.searchParams.set("selected", selected);
    if (version !== null) url.searchParams.set("version", version);
    try {
        const response = await fetch(url, {signal: active.signal, cache: "no-store", redirect: "error"});
        if (!response.ok) throw new Error("Обзор недоступен");
        const data = await response.json();
        if (request !== active) return;
        if (data.run !== run) throw new Error("Другой запуск обзора");
        loadFailed = Boolean(data.error);
        if (loadFailed) {
            freshness("stale", rows === null ? "Чтение задач не удалось" : "Обновление задержано", data.error);
        } else {
            version = data.version;
            detail = data.task;
            if (data.tasks) updateRows(data.tasks);
            freshness("ready", "Сверено", "", data.checked_at);
        }
        renderCard();
    } catch {
        if (request !== active) return;
        loadFailed = true;
        freshness("offline", "Связь с обзором потеряна", "Проверьте, работает ли команда запуска обзора.");
        renderCard();
    } finally {
        clearTimeout(timeout);
        if (request === active && !document.hidden) {
            timer = setTimeout(() => {
                if (!document.hidden) refresh();
            }, interval);
        }
    }
}

byId("tree-view").addEventListener("click", () => switchView("tree"));
byId("all-view").addEventListener("click", () => switchView("all"));
search.addEventListener("input", renderList);
for (const control of [rootsOnly, statusFilter, parentFilter]) control.addEventListener("change", renderList);
window.addEventListener("hashchange", () => {
    readSelection();
    refresh();
    if (selected && matchMedia("(max-width: 760px)").matches) cardPanel.scrollIntoView({block: "start"});
});
document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(timer);
    else refresh();
});

readSelection();
refresh();
