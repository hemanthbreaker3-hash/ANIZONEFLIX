/**
 * ANIZONEFLIX — Frontend interactions
 * Smooth scrolling, row navigation, lazy polish
 */
document.addEventListener('DOMContentLoaded', () => {
    // Horizontal row scroll buttons (optional enhancement)
    document.querySelectorAll('.row-wrapper').forEach(wrapper => {
        const row = wrapper.querySelector('.row-scroll');
        if (!row) return;

        const prev = document.createElement('button');
        prev.className = 'row-nav prev';
        prev.setAttribute('aria-label', 'Scroll left');
        prev.innerHTML = '<i class="fas fa-chevron-left"></i>';

        const next = document.createElement('button');
        next.className = 'row-nav next';
        next.setAttribute('aria-label', 'Scroll right');
        next.innerHTML = '<i class="fas fa-chevron-right"></i>';

        wrapper.appendChild(prev);
        wrapper.appendChild(next);

        const scrollBy = () => Math.min(row.clientWidth * 0.8, 400);

        prev.addEventListener('click', () => {
            row.scrollBy({ left: -scrollBy(), behavior: 'smooth' });
        });
        next.addEventListener('click', () => {
            row.scrollBy({ left: scrollBy(), behavior: 'smooth' });
        });
    });

    // Soft focus on search inputs when landing on /search
    const searchInput = document.querySelector('.search-box-large input, .header-search input');
    if (searchInput && window.location.pathname === '/search' && !searchInput.value) {
        setTimeout(() => searchInput.focus(), 120);
    }

    // Intersection observer for fade-in cards
    if ('IntersectionObserver' in window) {
        const io = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('fade-in');
                    io.unobserve(entry.target);
                }
            });
        }, { rootMargin: '40px', threshold: 0.08 });

        document.querySelectorAll('.anime-card, .category-card').forEach(el => {
            el.style.opacity = '0';
            io.observe(el);
        });
    }

    // Prevent empty search submit spam
    document.querySelectorAll('form[action="/search"]').forEach(form => {
        form.addEventListener('submit', (e) => {
            const input = form.querySelector('input[name="q"]');
            if (input && !input.value.trim()) {
                // Allow empty to show browse view
            }
        });
    });
});
