/**
 * Dashboard JavaScript for LiteLLM Router Platform
 */

// API base URL
const API_BASE = '/api';

// Current active tab
let currentTab = 'dashboard';

// Initialize dashboard on page load
document.addEventListener('DOMContentLoaded', function() {
    initializeNavigation();
    loadDashboard();
});

// Navigation handling
function initializeNavigation() {
    document.querySelectorAll('.nav-link').forEach(link => {
        link.addEventListener('click', function(e) {
            e.preventDefault();
            const tab = this.getAttribute('href').substring(1);
            showTab(tab);
        });
    });
}

function showTab(tabName) {
    // Hide all tabs
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.style.display = 'none';
    });
    
    // Show selected tab
    const selectedTab = document.getElementById(tabName);
    if (selectedTab) {
        selectedTab.style.display = 'block';
        currentTab = tabName;
        
        // Update nav links
        document.querySelectorAll('.nav-link').forEach(link => {
            link.classList.remove('active');
        });
        document.querySelector(`[href="#${tabName}"]`).classList.add('active');
        
        // Load tab content
        loadTabContent(tabName);
    }
}

function loadTabContent(tabName) {
    switch(tabName) {
        case 'dashboard':
            loadDashboard();
            break;
        case 'routing-rules':
            loadRoutingRules();
            break;
        case 'modules':
            loadModules();
            break;
        case 'cooldowns':
            loadCooldowns();
            break;
    }
}

// Dashboard functions
async function loadDashboard() {
    await Promise.all([
        refreshHealth(),
        loadStats()
    ]);
}

async function refreshHealth() {
    try {
        const response = await fetch(`${API_BASE}/health`);
        const health = await response.json();
        
        const healthCard = document.getElementById('health-card');
        const healthStatus = document.getElementById('health-status');
        
        // Update card styling based on health
        healthCard.className = `card status-card status-${health.status === 'healthy' ? 'healthy' : 'unhealthy'}`;
        
        // Build health status HTML
        let statusHtml = `
            <div class="row">
                <div class="col-md-6">
                    <strong>Overall Status:</strong> 
                    <span class="badge bg-${health.status === 'healthy' ? 'success' : 'danger'}">
                        ${health.status.toUpperCase()}
                    </span>
                </div>
                <div class="col-md-6">
                    <strong>Database:</strong> 
                    <span class="badge bg-${health.database === 'connected' ? 'success' : 'danger'}">
                        ${health.database.toUpperCase()}
                    </span>
                </div>
            </div>
        `;
        
        if (health.modules && Object.keys(health.modules).length > 0) {
            statusHtml += '<div class="mt-3"><strong>Modules:</strong><br>';
            for (const [module, status] of Object.entries(health.modules)) {
                const badgeClass = status === 'valid' ? 'success' : 'warning';
                statusHtml += `<span class="badge bg-${badgeClass} me-1">${module}: ${status}</span>`;
            }
            statusHtml += '</div>';
        }
        
        if (health.errors && health.errors.length > 0) {
            statusHtml += '<div class="mt-3"><strong>Errors:</strong>';
            statusHtml += '<ul class="mb-0">';
            health.errors.forEach(error => {
                statusHtml += `<li class="text-danger">${error}</li>`;
            });
            statusHtml += '</ul></div>';
        }
        
        healthStatus.innerHTML = statusHtml;
        
    } catch (error) {
        console.error('Failed to load health status:', error);
        document.getElementById('health-status').innerHTML = 
            '<span class="text-danger">Failed to load health status</span>';
    }
}

async function loadStats() {
    try {
        const response = await fetch(`${API_BASE}/stats`);
        const stats = await response.json();
        
        // Update stats cards
        document.getElementById('rules-count').textContent = 
            `${stats.routing_rules.active} / ${stats.routing_rules.total}`;
        document.getElementById('modules-count').textContent = 
            `${stats.modules.active} / ${stats.modules.total}`;
        document.getElementById('cooldowns-count').textContent = stats.cooldowns.active;
        document.getElementById('schema-version').textContent = stats.schema_version;
        
    } catch (error) {
        console.error('Failed to load stats:', error);
    }
}

// Routing Rules functions
async function loadRoutingRules() {
    try {
        const response = await fetch(`${API_BASE}/routing-rules`);
        const rules = await response.json();
        
        let tableHtml = `
            <table class="table table-striped">
                <thead>
                    <tr>
                        <th>Model Name</th>
                        <th>Module</th>
                        <th>Priority</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
        `;
        
        if (rules.length === 0) {
            tableHtml += `
                <tr>
                    <td colspan="5" class="text-center">No routing rules configured</td>
                </tr>
            `;
        } else {
            rules.forEach(rule => {
                const statusBadge = rule.enabled ? 
                    '<span class="badge bg-success">Enabled</span>' : 
                    '<span class="badge bg-secondary">Disabled</span>';
                
                tableHtml += `
                    <tr class="${rule.enabled ? '' : 'rule-disabled'}">
                        <td><code>${rule.model_name}</code></td>
                        <td>${rule.module_name}</td>
                        <td>${rule.priority}</td>
                        <td>${statusBadge}</td>
                        <td>
                            <button class="btn btn-sm btn-danger" onclick="deleteRule('${rule.model_name}')">
                                <i class="fas fa-trash"></i>
                            </button>
                        </td>
                    </tr>
                `;
            });
        }
        
        tableHtml += '</tbody></table>';
        document.getElementById('rules-table').innerHTML = tableHtml;
        
        // Load modules for the add rule modal
        await loadModulesForModal();
        
    } catch (error) {
        console.error('Failed to load routing rules:', error);
        document.getElementById('rules-table').innerHTML = 
            '<div class="alert alert-danger">Failed to load routing rules</div>';
    }
}

async function loadModulesForModal() {
    try {
        const response = await fetch(`${API_BASE}/modules`);
        const modules = await response.json();
        
        const select = document.getElementById('moduleName');
        select.innerHTML = '<option value="">Select a module</option>';
        
        modules.forEach(module => {
            if (module.enabled) {
                select.innerHTML += `<option value="${module.module_name}">${module.display_name}</option>`;
            }
        });
        
    } catch (error) {
        console.error('Failed to load modules for modal:', error);
    }
}

async function addRule() {
    const form = document.getElementById('addRuleForm');
    const formData = new FormData(form);
    
    const rule = {
        model_name: document.getElementById('modelName').value,
        module_name: document.getElementById('moduleName').value,
        priority: parseInt(document.getElementById('priority').value),
        enabled: document.getElementById('enabled').checked
    };
    
    if (!rule.model_name || !rule.module_name) {
        alert('Please fill in all required fields');
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/routing-rules`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(rule)
        });
        
        if (response.ok) {
            // Close modal and refresh rules
            bootstrap.Modal.getInstance(document.getElementById('addRuleModal')).hide();
            form.reset();
            loadRoutingRules();
            loadStats(); // Refresh stats
        } else {
            const error = await response.json();
            alert(`Failed to add rule: ${error.detail}`);
        }
        
    } catch (error) {
        console.error('Failed to add rule:', error);
        alert('Failed to add rule');
    }
}

async function deleteRule(modelName) {
    if (!confirm(`Are you sure you want to delete the routing rule for "${modelName}"?`)) {
        return;
    }
    
    try {
        const response = await fetch(`${API_BASE}/routing-rules/${encodeURIComponent(modelName)}`, {
            method: 'DELETE'
        });
        
        if (response.ok) {
            loadRoutingRules();
            loadStats(); // Refresh stats
        } else {
            const error = await response.json();
            alert(`Failed to delete rule: ${error.detail}`);
        }
        
    } catch (error) {
        console.error('Failed to delete rule:', error);
        alert('Failed to delete rule');
    }
}