/* Battery history page. */
(function () {
    'use strict';
    const interval = document.getElementById('battery-interval');
    const cards = document.getElementById('battery-cards');
    const loading = document.getElementById('battery-loading');
    const error = document.getElementById('battery-error');
    const empty = document.getElementById('battery-empty');
    const template = document.getElementById('battery-card-template');
    const charts = new Map();
    let requestNumber = 0;

    function formatTime(value) {
        if (!value) return 'unknown time';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
    }

    function formatVoltageTick(value) {
        return Number(value).toFixed(2);
    }

    function createTimeTickFormatter(points) {
        const firstIndexByDate = new Map();
        const dates = points.map((point) => {
            const date = new Date(point.timestamp);
            if (Number.isNaN(date.getTime())) return null;
            return date;
        });
        dates.forEach((date, index) => {
            if (!date) return;
            const dateKey = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
            if (!firstIndexByDate.has(dateKey)) firstIndexByDate.set(dateKey, index);
        });

        return (value, index) => {
            const timestamp = points[index]?.timestamp;
            if (!timestamp) return '';
            const date = dates[index];
            if (!date) return timestamp;

            const dateKey = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
            const time = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
            if (firstIndexByDate.get(dateKey) !== index) return time;

            return `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')} ${time}`;
        };
    }

    function statusText(status) {
        if (status === 'ok') return ['Reporting', 'bg-success', 'Node is responding.'];
        if (status === 'not_responding') return ['Not responding', 'bg-warning text-dark', 'No current value; showing the last known reading.'];
        return ['No current value', 'bg-secondary', 'This node has not reported a battery value yet.'];
    }

    function clearCharts() {
        charts.forEach((chart) => chart.destroy());
        charts.clear();
    }

    function renderChart(canvas, node, index) {
        if (typeof Chart === 'undefined' || !node.points.length) return;
        const textColor = getComputedStyle(document.documentElement).getPropertyValue('--text-color').trim() || '#212529';
        const muted = getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() || '#6c757d';
        const chart = new Chart(canvas.getContext('2d'), {
            type: 'line',
            data: {
                labels: node.points.map((point) => point.timestamp),
                datasets: [{
                    label: 'Voltage (V)',
                    data: node.points.map((point) => point.voltage),
                    borderColor: '#0d6efd',
                    backgroundColor: 'rgba(13, 110, 253, 0.12)',
                    pointRadius: 2,
                    pointHoverRadius: 6,
                    tension: 0.2,
                    fill: true,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                spanGaps: false,
                animation: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { title: (items) => formatTime(items[0].label) } },
                },
                scales: {
                    x: {
                        ticks: {
                            color: muted,
                            maxRotation: 0,
                            autoSkip: true,
                            callback: createTimeTickFormatter(node.points),
                        },
                        grid: { color: `${muted}33` },
                    },
                    y: {
                        title: { display: true, text: 'Voltage (V)', color: textColor },
                        ticks: { color: muted, callback: formatVoltageTick },
                        grid: { color: `${muted}33` },
                    },
                },
            },
        });
        charts.set(`${index}`, chart);
    }

    function render(data) {
        clearCharts();
        cards.replaceChildren();
        empty.classList.toggle('d-none', data.nodes.length !== 0);
        data.nodes.forEach((node, index) => {
            const fragment = template.content.cloneNode(true);
            const column = fragment.querySelector('.battery-node-column');
            const name = fragment.querySelector('.battery-node-name');
            const badge = fragment.querySelector('.battery-status');
            const current = fragment.querySelector('.battery-current');
            const currentTime = fragment.querySelector('.battery-current-time');
            const message = fragment.querySelector('.battery-status-message');
            const canvas = fragment.querySelector('.battery-chart');
            const readings = fragment.querySelector('.battery-readings');
            name.textContent = node.name;
            canvas.setAttribute('aria-label', `${node.name} voltage history`);
            const status = statusText(node.status);
            badge.textContent = status[0];
            badge.className = `badge battery-status ${status[1]}`;
            message.textContent = status[2];
            if (node.current) {
                current.textContent = `${Number(node.current.voltage).toFixed(3)} V`;
                currentTime.textContent = `Last reading: ${formatTime(node.current.timestamp)}`;
            } else {
                current.textContent = '—';
                currentTime.textContent = 'No reading available';
            }
            if (node.points.length) {
                node.points.forEach((point) => {
                    const item = document.createElement('li');
                    item.textContent = `${formatTime(point.timestamp)} — ${Number(point.voltage).toFixed(3)} V`;
                    readings.appendChild(item);
                });
            } else {
                const item = document.createElement('li');
                item.textContent = 'No readings in the selected interval.';
                readings.appendChild(item);
            }
            cards.appendChild(fragment);
            renderChart(cards.lastElementChild.querySelector('.battery-chart'), node, index);
        });
    }

    async function load() {
        const currentRequest = ++requestNumber;
        loading.classList.remove('d-none');
        error.classList.add('d-none');
        interval.disabled = true;
        try {
            const response = await fetch(`/api/battery?interval=${encodeURIComponent(interval.value)}`);
            if (!response.ok) throw new Error('Unable to load battery readings.');
            const data = await response.json();
            if (currentRequest !== requestNumber) return;
            render(data);
        } catch (err) {
            if (currentRequest !== requestNumber) return;
            error.textContent = err.message || 'Unable to load battery readings.';
            error.classList.remove('d-none');
        } finally {
            if (currentRequest === requestNumber) {
                loading.classList.add('d-none');
                interval.disabled = false;
            }
        }
    }

    interval.addEventListener('change', load);
    load();
})();
