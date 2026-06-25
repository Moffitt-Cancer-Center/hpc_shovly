document.addEventListener('DOMContentLoaded', () => {
    const activeJobsElem  = document.getElementById('active-jobs');
    const awsCostElem     = document.getElementById('aws-cost');
    const azureCostElem   = document.getElementById('azure-cost');
    const lastUpdatedElem = document.getElementById('last-updated');
    const jobsTableBody   = document.getElementById('jobs-table-body');

    let costChart, instanceChart, historicalChart;

    const chartColors = {
        aws: 'rgba(255, 159, 64, 0.8)',
        azure: 'rgba(54, 162, 235, 0.8)',
        pieSlice: [
            'rgba(255, 99, 132, 0.8)',
            'rgba(54, 162, 235, 0.8)',
            'rgba(255, 206, 86, 0.8)',
            'rgba(75, 192, 192, 0.8)',
            'rgba(153, 102, 255, 0.8)',
            'rgba(255, 159, 64, 0.8)'
        ]
    };

    function initializeCharts() {
        const costCtx = document.getElementById('cost-chart').getContext('2d');
        costChart = new Chart(costCtx, {
            type: 'bar',
            data: {
                labels: ['AWS', 'Azure'],
                datasets: [{
                    label: 'Projected Total Cost ($)',
                    data: [0, 0],
                    backgroundColor: [chartColors.aws, chartColors.azure],
                    borderColor: [chartColors.aws.replace('0.8', '1'), chartColors.azure.replace('0.8', '1')],
                    borderWidth: 1
                }]
            },
            options: {
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: {
                            color: '#e0e0e0'
                        },
                        grid: {
                            color: 'rgba(224, 224, 224, 0.2)'
                        }
                    },
                    x: {
                        ticks: {
                            color: '#e0e0e0'
                        },
                        grid: {
                            display: false
                        }
                    }
                },
                plugins: {
                    legend: {
                        display: false
                    }
                }
            }
        });

        const instanceCtx = document.getElementById('instance-chart').getContext('2d');
        instanceChart = new Chart(instanceCtx, {
            type: 'doughnut',
            data: {
                labels: [],
                datasets: [{
                    data: [],
                    backgroundColor: chartColors.pieSlice,
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: {
                        position: 'top',
                        labels: {
                            color: '#e0e0e0'
                        }
                    }
                }
            }
        });
    }

    async function updateDashboard() {
        try {
            const response = await fetch('/api/metrics');
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();

            // Update KPIs
            activeJobsElem.textContent  = data.active_jobs;
            awsCostElem.textContent     = `$${data.projected_cost_aws.toFixed(2)}`;
            azureCostElem.textContent   = `$${data.projected_cost_azure.toFixed(2)}`;
            lastUpdatedElem.textContent = data.last_updated || 'Never';

            // Update Cost Chart
            costChart.data.datasets[0].data = [data.projected_cost_aws, data.projected_cost_azure];
            costChart.update();

            // Update Instance Distribution Chart (by AWS instance type)
            const instanceCounts = data.job_details.reduce((acc, job) => {
                acc[job.aws_instance] = (acc[job.aws_instance] || 0) + 1;
                return acc;
            }, {});
            instanceChart.data.labels = Object.keys(instanceCounts);
            instanceChart.data.datasets[0].data = Object.values(instanceCounts);
            instanceChart.update();

            // Update Jobs Table
            jobsTableBody.innerHTML = '';
            data.job_details.forEach(job => {
                const row = document.createElement('tr');
                const gpuLabel       = job.gpu_count > 0 ? `${job.gpu_count}x ${job.gpu_model}` : '—';
                const timeLimitLabel = job.time_limit_min > 0
                    ? `${Math.floor(job.time_limit_min / 60)}h ${job.time_limit_min % 60}m`
                    : '—';
                row.innerHTML = `
                    <td>${job.job_id}</td>
                    <td>${job.cluster}</td>
                    <td>${job.cpus}</td>
                    <td>${job.mem_gb}</td>
                    <td>${gpuLabel}</td>
                    <td>${timeLimitLabel}</td>
                    <td>${job.aws_instance}</td>
                    <td>$${job.aws_total.toFixed(2)}</td>
                    <td>${job.azure_instance}</td>
                    <td>$${job.azure_total.toFixed(2)}</td>
                `;
                jobsTableBody.appendChild(row);
            });

            // Keep cluster dropdown in sync
            const clusterSelect  = document.getElementById('hist-cluster');
            const knownClusters  = new Set([...clusterSelect.options].map(o => o.value));
            data.job_details.forEach(job => {
                if (!knownClusters.has(job.cluster)) {
                    const opt = document.createElement('option');
                    opt.value = job.cluster;
                    opt.textContent = job.cluster;
                    clusterSelect.appendChild(opt);
                    knownClusters.add(job.cluster);
                }
            });

            applySortIfActive('jobs-table');

        } catch (error) {
            console.error("Failed to fetch metrics:", error);
        }
    }

    try {
        initializeCharts();
    } catch (e) {
        console.error('Chart.js failed to initialize:', e);
        document.getElementById('cost-chart').parentElement.innerHTML = '<p style="color:#a0a0a0;text-align:center">Charts unavailable</p>';
        document.getElementById('instance-chart').parentElement.innerHTML = '<p style="color:#a0a0a0;text-align:center">Charts unavailable</p>';
    }
    updateDashboard();
    setInterval(updateDashboard, 5000);

    initSortableTable('jobs-table');
    initSortableTable('users-table');

    // ----------------------------------------------------------------
    // Historical Cost Calculator
    // ----------------------------------------------------------------

    function initHistoricalChart() {
        const ctx = document.getElementById('historical-chart').getContext('2d');
        historicalChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    {
                        label: 'AWS Daily Cost ($)',
                        data: [],
                        borderColor: chartColors.aws.replace('0.8', '1'),
                        backgroundColor: chartColors.aws.replace('0.8', '0.15'),
                        tension: 0.3,
                        fill: true,
                        pointRadius: 3,
                    },
                    {
                        label: 'Azure Daily Cost ($)',
                        data: [],
                        borderColor: chartColors.azure.replace('0.8', '1'),
                        backgroundColor: chartColors.azure.replace('0.8', '0.15'),
                        tension: 0.3,
                        fill: true,
                        pointRadius: 3,
                    }
                ]
            },
            options: {
                responsive: true,
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: { color: '#e0e0e0', callback: v => `$${v.toLocaleString()}` },
                        grid: { color: 'rgba(224,224,224,0.2)' }
                    },
                    x: {
                        ticks: { color: '#e0e0e0', maxTicksLimit: 14 },
                        grid: { display: false }
                    }
                },
                plugins: {
                    legend: { labels: { color: '#e0e0e0' } },
                    tooltip: {
                        callbacks: {
                            label: ctx => ` $${ctx.parsed.y.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}`
                        }
                    }
                }
            }
        });
    }

    function formatCurrency(n) {
        return '$' + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    // ----------------------------------------------------------------
    // Sortable table utility
    // ----------------------------------------------------------------
    const _sortState = {};  // { tableId: { colIndex, asc } }

    function _parseSortVal(text) {
        const s = text.trim().replace(/[$,]/g, '');
        if (s === '\u2014' || s === '' || s === '-') return null; // em-dash / empty → sort to end
        const tm = s.match(/^(\d+)h\s+(\d+)m$/);
        if (tm) return parseInt(tm[1]) * 60 + parseInt(tm[2]);    // "2h 30m" → minutes
        const n = parseFloat(s);
        return isNaN(n) ? s.toLowerCase() : n;
    }

    function sortTable(tableId, colIndex, headers, forceAsc) {
        const table = document.getElementById(tableId);
        if (!table) return;
        const tbody = table.querySelector('tbody');
        if (!tbody) return;
        const rows = Array.from(tbody.querySelectorAll('tr'));

        const prev = _sortState[tableId] || {};
        const asc  = forceAsc !== undefined ? forceAsc
                   : (prev.colIndex === colIndex ? !prev.asc : true);
        _sortState[tableId] = { colIndex, asc };

        rows.sort((a, b) => {
            const av = _parseSortVal(a.cells[colIndex]?.textContent || '');
            const bv = _parseSortVal(b.cells[colIndex]?.textContent || '');
            if (av === null && bv === null) return 0;
            if (av === null) return  1;
            if (bv === null) return -1;
            if (typeof av === 'number' && typeof bv === 'number') return asc ? av - bv : bv - av;
            return asc ? String(av).localeCompare(String(bv))
                       : String(bv).localeCompare(String(av));
        });

        rows.forEach(r => tbody.appendChild(r));

        headers.forEach(th => { const a = th.querySelector('.sort-arrow'); if (a) a.textContent = ''; });
        const activeTh = headers[colIndex];
        if (activeTh) {
            let arrow = activeTh.querySelector('.sort-arrow');
            if (!arrow) { arrow = document.createElement('span'); arrow.className = 'sort-arrow'; activeTh.appendChild(arrow); }
            arrow.textContent = asc ? ' \u25b2' : ' \u25bc';
        }
    }

    function initSortableTable(tableId) {
        const table = document.getElementById(tableId);
        if (!table) return;
        const headers = Array.from(table.querySelectorAll('thead th'));
        headers.forEach((th, i) => {
            th.classList.add('sortable');
            th.addEventListener('click', () => sortTable(tableId, i, headers));
        });
    }

    function applySortIfActive(tableId) {
        const state = _sortState[tableId];
        if (!state) return;
        const table = document.getElementById(tableId);
        if (!table) return;
        sortTable(tableId, state.colIndex, Array.from(table.querySelectorAll('thead th')), state.asc);
    }

    async function fetchHistorical() {
        const start   = document.getElementById('hist-start').value;
        const end     = document.getElementById('hist-end').value;
        const cluster = document.getElementById('hist-cluster').value;

        const params = new URLSearchParams();
        if (start)           params.set('start',   start);
        if (end)             params.set('end',     end);
        if (cluster !== 'all') params.set('cluster', cluster);

        try {
            const res  = await fetch('/api/historical?' + params.toString());
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();

            document.getElementById('hist-total-jobs').textContent     = data.total_jobs.toLocaleString();
            document.getElementById('hist-aws-total').textContent      = formatCurrency(data.total_aws);
            document.getElementById('hist-azure-total').textContent    = formatCurrency(data.total_azure);
            document.getElementById('hist-compute-hours').textContent  = data.total_compute_hours.toLocaleString();

            if (!historicalChart) initHistoricalChart();
            historicalChart.data.labels                   = data.daily.map(d => d.date);
            historicalChart.data.datasets[0].data         = data.daily.map(d => d.aws_total);
            historicalChart.data.datasets[1].data         = data.daily.map(d => d.azure_total);
            historicalChart.update();
        } catch (e) {
            console.error('Historical fetch failed:', e);
        }
    }

    async function fetchTopUsers() {
        const start   = document.getElementById('hist-start').value;
        const end     = document.getElementById('hist-end').value;
        const cluster = document.getElementById('hist-cluster').value;

        const params = new URLSearchParams();
        if (start)           params.set('start',   start);
        if (end)             params.set('end',     end);
        if (cluster !== 'all') params.set('cluster', cluster);

        const statusEl = document.getElementById('users-status');
        const tableEl  = document.getElementById('users-table');
        const tbody    = document.getElementById('users-table-body');

        try {
            const res  = await fetch('/api/top-users?' + params.toString());
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();

            if (!data.users || data.users.length === 0) {
                statusEl.textContent   = 'No historical data found for the selected range.';
                statusEl.style.display = 'block';
                tableEl.style.display  = 'none';
                return;
            }

            statusEl.style.display = 'none';
            tableEl.style.display  = 'table';
            tbody.innerHTML        = '';
            data.users.forEach((user, i) => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${i + 1}</td>
                    <td>${user.username}</td>
                    <td>${user.job_count.toLocaleString()}</td>
                    <td>${user.total_hours.toLocaleString()}</td>
                    <td>${formatCurrency(user.aws_total)}</td>
                    <td>${formatCurrency(user.azure_total)}</td>
                `;
                tbody.appendChild(tr);
            });
            applySortIfActive('users-table');
        } catch (e) {
            console.error('Top-users fetch failed:', e);
        }
    }

    // Wire Calculate button
    document.getElementById('hist-calc-btn').addEventListener('click', async () => {
        const btn = document.getElementById('hist-calc-btn');
        btn.disabled    = true;
        btn.textContent = 'Loading…';
        await Promise.all([fetchHistorical(), fetchTopUsers()]);
        btn.disabled    = false;
        btn.textContent = 'Calculate';
    });

    // Pre-fill date range to last 30 days
    const today       = new Date();
    const thirtyAgo   = new Date(today);
    thirtyAgo.setDate(today.getDate() - 30);
    document.getElementById('hist-end').value   = today.toISOString().split('T')[0];
    document.getElementById('hist-start').value = thirtyAgo.toISOString().split('T')[0];

    // Populate cluster dropdown from /api/clusters
    fetch('/api/clusters').then(r => r.json()).then(data => {
        const sel    = document.getElementById('hist-cluster');
        const known  = new Set([...sel.options].map(o => o.value));
        (data.clusters || []).forEach(name => {
            if (!known.has(name)) {
                const opt = document.createElement('option');
                opt.value = name; opt.textContent = name;
                sel.appendChild(opt);
            }
        });
    }).catch(() => {});
});
