from __future__ import annotations

ROUTE_TRACE_JS = r"""
const cards = Array.from(document.querySelectorAll('.request-card'));
const search = document.getElementById('search');
const buttons = Array.from(document.querySelectorAll('button[data-filter]'));
let activeFilter = 'all';
const fullPreviewPopover = document.createElement('div');
fullPreviewPopover.className = 'full-preview-popover';
fullPreviewPopover.setAttribute('role', 'tooltip');
document.body.appendChild(fullPreviewPopover);
let activePreviewTarget = null;
let hidePreviewTimer = null;

function positionFullPreview(event, target) {
  const margin = 12;
  const targetRect = target.getBoundingClientRect();
  const desiredLeft = event ? event.clientX + 14 : targetRect.left;
  const desiredTop = event ? event.clientY + 16 : targetRect.bottom + 8;
  const popoverRect = fullPreviewPopover.getBoundingClientRect();
  const left = Math.max(
    margin,
    Math.min(desiredLeft, window.innerWidth - popoverRect.width - margin)
  );
  const top = Math.max(
    margin,
    Math.min(desiredTop, window.innerHeight - popoverRect.height - margin)
  );
  fullPreviewPopover.style.left = `${left}px`;
  fullPreviewPopover.style.top = `${top}px`;
}

function showFullPreview(target, event) {
  const text = target.dataset.fullPreview || '';
  if (!text.trim()) return;
  clearTimeout(hidePreviewTimer);
  activePreviewTarget = target;
  fullPreviewPopover.textContent = text;
  fullPreviewPopover.classList.add('visible');
  positionFullPreview(event, target);
}

function hideFullPreview() {
  fullPreviewPopover.classList.remove('visible');
  activePreviewTarget = null;
}

function scheduleFullPreviewHide() {
  clearTimeout(hidePreviewTimer);
  hidePreviewTimer = setTimeout(() => {
    if (fullPreviewPopover.matches(':hover')) return;
    if (activePreviewTarget && activePreviewTarget.matches(':hover')) return;
    hideFullPreview();
  }, 120);
}

for (const target of document.querySelectorAll('[data-full-preview]')) {
  target.addEventListener('mouseenter', event => showFullPreview(target, event));
  target.addEventListener('mousemove', event => positionFullPreview(event, target));
  target.addEventListener('mouseleave', scheduleFullPreviewHide);
  target.addEventListener('focus', () => showFullPreview(target, null));
  target.addEventListener('blur', scheduleFullPreviewHide);
}
fullPreviewPopover.addEventListener('mouseenter', () => clearTimeout(hidePreviewTimer));
fullPreviewPopover.addEventListener('mouseleave', scheduleFullPreviewHide);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') hideFullPreview();
});
window.addEventListener('scroll', hideFullPreview, true);

function applyFilters() {
  const q = (search.value || '').trim().toLowerCase();
  for (const card of cards) {
    const textMatch = !q || (card.dataset.search || '').includes(q);
    const filterMatch =
      activeFilter === 'all' ||
      (activeFilter === 'fallback' && card.dataset.fallback === 'true') ||
      (activeFilter === 'image' && card.dataset.image === 'true');
    card.classList.toggle('hidden', !(textMatch && filterMatch));
  }
}

search.addEventListener('input', applyFilters);
for (const button of buttons) {
  button.addEventListener('click', () => {
    activeFilter = button.dataset.filter;
    for (const item of buttons) item.classList.toggle('active', item === button);
    applyFilters();
  });
}
document.getElementById('expand').addEventListener('click', () => cards.forEach(card => card.open = true));
document.getElementById('collapse').addEventListener('click', () => cards.forEach(card => card.open = false));
"""
