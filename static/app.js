document.addEventListener('DOMContentLoaded', () => {
    const activeJobsElem = document.getElementById('active-jobs');
    const awsCostElem = document.getElementById('aws-cost');
    const azureCostElem = document.getElementById('azure-cost');
    const lastUpdatedElem = document.getElementById('last-updated');
    const jobsTableBody = document.getElementById('jobs-table-body');

    let costChart, instanceChart;

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
                    label: 'Hourly Cost',
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
            activeJobsElem.textContent = data.active_jobs;
            awsCostElem.textContent = `$${data.hourly_cost_aws.toFixed(2)}`;
            azureCostElem.textContent = `$${data.hourly_cost_azure.toFixed(2)}`;
            lastUpdatedElem.textContent = data.last_updated || 'Never';

            // Update Cost Chart
            costChart.data.datasets[0].data = [data.hourly_cost_aws, data.hourly_cost_azure];
            costChart.update();

            // Update Instance Distribution Chart
            const instanceCounts = data.job_details.reduce((acc, job) => {
                acc[job.mapped_instance] = (acc[job.mapped_instance] || 0) + 1;
                return acc;
            }, {});
            instanceChart.data.labels = Object.keys(instanceCounts);
            instanceChart.data.datasets[0].data = Object.values(instanceCounts);
            instanceChart.update();

            // Update Jobs Table
            jobsTableBody.innerHTML = ''; // Clear existing rows
            data.job_details.forEach(job => {
                const row = document.createElement('tr');
                const gpuLabel = job.gpu_count > 0 ? `${job.gpu_count}x ${job.gpu_model}` : '—';
                row.innerHTML = `
                    <td>${job.job_id}</td>
                    <td>${job.cluster}</td>
                    <td>${job.cpus}</td>
                    <td>${job.mem_gb}</td>
                    <td>${gpuLabel}</td>
                    <td>${job.aws_instance}</td>
                    <td>$${job.aws_hourly.toFixed(3)}</td>
                    <td>${job.azure_instance}</td>
                    <td>$${job.azure_hourly.toFixed(3)}</td>
                `;
                jobsTableBody.appendChild(row);
            });

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
    updateDashboard(); // Initial load
    setInterval(updateDashboard, 5000); // Update every 5 seconds
});
