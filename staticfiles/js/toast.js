/**
 * PyRunner Toast Notification System
 */
const PyRunnerToast = {
    container: null,
    defaults: {
        duration: 5000,
        maxToasts: 5
    },

    init() {
        if (!this.container) {
            this.container = document.createElement('div');
            this.container.id = 'toast-container';
            this.container.className = 'fixed flex flex-col gap-3 pointer-events-none';
            this.container.style.cssText = 'top: 5rem; right: 1rem; z-index: 9999;';
            document.body.appendChild(this.container);
        }
    },

    show(message, type = 'info', duration = this.defaults.duration) {
        this.init();

        const toast = document.createElement('div');
        toast.className = this.getToastClasses(type);
        toast.innerHTML = this.getToastHTML(message, type);
        toast.style.animation = 'toastSlideIn 0.3s ease-out';

        const closeBtn = toast.querySelector('[data-toast-close]');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => this.dismiss(toast));
        }

        while (this.container.children.length >= this.defaults.maxToasts) {
            this.dismiss(this.container.firstChild);
        }

        this.container.appendChild(toast);

        if (duration > 0) {
            setTimeout(() => this.dismiss(toast), duration);
        }

        return toast;
    },

    dismiss(toast) {
        if (!toast || !toast.parentNode) return;
        toast.style.animation = 'toastSlideOut 0.2s ease-in forwards';
        setTimeout(() => toast.remove(), 200);
    },

    getToastClasses(type) {
        const base = 'pointer-events-auto max-w-sm w-full p-4 rounded-lg border shadow-lg flex items-start gap-3';
        const types = {
            success: 'bg-code-surface border-code-green/30 text-code-green',
            error: 'bg-code-surface border-code-red/30 text-code-red',
            warning: 'bg-code-surface border-code-yellow/30 text-code-yellow',
            info: 'bg-code-surface border-code-accent/30 text-code-accent'
        };
        return `${base} ${types[type] || types.info}`;
    },

    getToastHTML(message, type) {
        const icons = {
            success: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>',
            error: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>',
            warning: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>',
            info: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>'
        };

        return `
            <svg class="w-5 h-5 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                ${icons[type] || icons.info}
            </svg>
            <p class="flex-1 text-sm text-code-text">${message}</p>
            <button data-toast-close class="flex-shrink-0 text-code-muted hover:text-code-text transition-colors">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
                </svg>
            </button>
        `;
    },

    success(message, duration) { return this.show(message, 'success', duration); },
    error(message, duration) { return this.show(message, 'error', duration); },
    warning(message, duration) { return this.show(message, 'warning', duration); },
    info(message, duration) { return this.show(message, 'info', duration); }
};

window.Toast = PyRunnerToast;
