document.addEventListener('DOMContentLoaded', () => {
    setupAddToCartForm();
    setupClearCartForm();
    bindRemoveFromCartForms();
    initToastContainer();
    setupProrationHandlers();
});

function setupProrationHandlers() {
    const amountDueField = document.getElementById('amountDueField');
    const resetBtn = document.getElementById('resetAmountBtn');
    
    if (!amountDueField) return;
    
    // Function to get current cart total from UI
    function getCurrentCartTotal() {
        // Get the cart total from the table footer
        const cartTotalEl = document.getElementById('cartTotal');
        if (cartTotalEl) {
            // Extract numeric value from currency string
            return parseFloat(cartTotalEl.textContent.replace(/[^\d.]/g, ''));
        }
        
        // Fallback to hidden field if footer not available
        const originalTotalEl = document.getElementById('originalTotal');
        if (originalTotalEl) {
            return parseFloat(originalTotalEl.value);
        }
        
        // Final fallback
        return 0;
    }
    
    // Initialize with current total
    let currentTotal = getCurrentCartTotal();
    
    // Set initial values
    amountDueField.value = currentTotal.toFixed(2);
    amountDueField.dataset.originalTotal = currentTotal;
    
    // Validate amount due
    function validateAmountDue(enteredValue, currentTotal) {
        const amountDueValidation = document.getElementById('amountDueValidation');
        if (!amountDueValidation) return;
        
        const enteredAmount = parseFloat(enteredValue);
        
        amountDueValidation.classList.add('d-none');
        
        if (isNaN(enteredAmount)) {
            amountDueValidation.textContent = 'Please enter a valid number';
            amountDueValidation.classList.remove('d-none');
            return false;
        }
        
        if (enteredAmount < 0) {
            amountDueValidation.textContent = 'Amount cannot be negative';
            amountDueValidation.classList.remove('d-none');
            return false;
        }
        
        if (enteredAmount > currentTotal * 1.5) {
            amountDueValidation.textContent = 'Amount exceeds 150% of original total';
            amountDueValidation.classList.remove('d-none');
            return false;
        }
        
        return true;
    }
    
    // Input validation
    amountDueField.addEventListener('input', function() {
        validateAmountDue(this.value, currentTotal);
    });
    
    // Reset button functionality
    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            currentTotal = getCurrentCartTotal();
            amountDueField.value = currentTotal.toFixed(2);
            amountDueField.dataset.originalTotal = currentTotal;
            validateAmountDue(currentTotal, currentTotal);
        });
    }
    
    // Update when cart changes
    document.addEventListener('cartUpdated', () => {
        currentTotal = getCurrentCartTotal();
        
        // Update hidden field if exists
        const originalTotalEl = document.getElementById('originalTotal');
        if (originalTotalEl) {
            originalTotalEl.value = currentTotal;
        }
        
        // Only reset payment field if not manually modified
        if (parseFloat(amountDueField.value) === amountDueField.dataset.originalTotal) {
            amountDueField.value = currentTotal.toFixed(2);
        }
        
        amountDueField.dataset.originalTotal = currentTotal;
        validateAmountDue(amountDueField.value, currentTotal);
    });
    
    // Initial validation
    validateAmountDue(amountDueField.value, currentTotal);
}

function validateAmountDue(currentValue, originalTotal) {
    const payBtn = document.querySelector('button[name="process_payment"]');
    const validationMsg = document.getElementById('amountDueValidation');
    
    if (!payBtn || !validationMsg) return;
    
    const currentAmount = parseFloat(currentValue) || 0;
    
    if (currentAmount > originalTotal) {
        validationMsg.textContent = 'Cannot exceed original total';
        validationMsg.classList.remove('d-none');
        payBtn.disabled = true;
    } else {
        validationMsg.classList.add('d-none');
        payBtn.disabled = currentAmount === 0;
    }
}

function setupAddToCartForm() {
    const form = document.getElementById('addToCartForm');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        clearFeedback('cartFeedback');

        const formData = new FormData(form);
        const productId = formData.get('product_id');
        
        try {
            const response = await fetch('/api/add_to_cart', {
                method: 'POST',
                body: formData,
                headers: { 'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': getCSRFToken() 
                 }
            });

            const data = await response.json();
            refreshCart();

            if (data.status === 'success') {
                showToast(`✅ ${data.message}`, 'success');
                form.reset();
                document.getElementById('product').value = '';
                document.getElementById('productSearch').value = '';
                document.getElementById('searchResults').innerHTML = '';
            } else {
                (data.errors || [data.message]).forEach(msg => 
                    showInlineAlert(msg, 'danger')
                );
            }
        } catch (error) {
            console.error('Error:', error);
            showToast('⚠️ An unexpected error occurred.', 'danger');
        }
    });
}

function setupClearCartForm() {
    const form = document.getElementById('clearCartForm');
    if (!form) return;
    
    const submitBtn = form.querySelector('button[type="submit"]');
    const originalContent = submitBtn.innerHTML;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        // Save original content and update button state
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Clearing...';
        submitBtn.disabled = true;

        try {
            const response = await fetch(form.action, {
                method: 'POST',
                headers: { 
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRF-Token': getCSRFToken()  // Important security addition
                }
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.message || 'Failed to clear cart');
            }

            const data = await response.json();
            if (data.status === 'success') {
                refreshCart();
                refreshPaymentForm();
                showToast(`✅ ${data.message}`, 'success');
            } else {
                showToast(`⚠️ ${data.message}`, 'warning');
            }
        } catch (error) {
            console.error('Clear cart error:', error);
            showToast(`⚠️ ${error.message || 'Failed to clear cart'}`, 'danger');
        } finally {
            // Restore original button state
            submitBtn.innerHTML = originalContent;
            submitBtn.disabled = false;
        }
    });
}

function bindRemoveFromCartForms() {
    document.addEventListener('click', async (e) => {
        const removeBtn = e.target.closest('.remove-cart-item');
        if (!removeBtn) return;
        e.preventDefault();
        
        const form = removeBtn.closest('form');
        const originalContent = removeBtn.innerHTML;
        
        if (!confirm('Are you sure you want to remove this item from the cart?')) return;
        
        // Update button state
        removeBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';
        removeBtn.disabled = true;

        try {
            const response = await fetch(form.action, {
                method: 'POST',
                headers: { 
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRF-Token': getCSRFToken()  // Important security addition
                }
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.message || 'Failed to remove item');
            }

            const data = await response.json();
            refreshCart();
            
            if (data.status === 'success') {
                showToast(`✅ ${data.message}`, 'success');
            } else {
                showToast(`⚠️ ${data.message}`, 'warning');
            }
        } catch (error) {
            console.error('Remove item error:', error);
            showToast(`⚠️ ${error.message || 'Failed to remove item'}`, 'danger');
        } finally {
            // Restore original button state
            removeBtn.innerHTML = originalContent;
            removeBtn.disabled = false;
        }
    });
}

// Utility function to get CSRF token from meta tag
function getCSRFToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || '';
}

function refreshCart() {
    fetch('/cart/partial')
        .then(res => res.text())
        .then(html => {
            const container = document.getElementById('cartContainer');
            if (!container) return;
            
            container.innerHTML = html;
            updateCartTotal();
            bindPrescriptionRequirement();
            initTooltips();
            
            // Notify other components
            document.dispatchEvent(new CustomEvent('cartUpdated'));

            // Update cart counter
            const hiddenCount = document.getElementById('cartCountHidden');
            const badge = document.getElementById('cartCounter');
            if (hiddenCount && badge) {
                badge.textContent = hiddenCount.value;
                badge.classList.toggle('d-none', hiddenCount.value == 0);
            }
        });
}

// Initialize Bootstrap tooltips
function initTooltips() {
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(el => new bootstrap.Tooltip(el));
}

function updateCartTotal() {
    fetch('/cart/total')
        .then(res => res.json())
        .then(data => {
            // Parse formatted total (e.g., "1,234.56")
            const totalAmount = parseFloat(data.total.replace(/,/g, ''));
            
            // Update hidden field for original total
            const originalTotalEl = document.getElementById('originalTotal');
            if (originalTotalEl) {
                originalTotalEl.value = totalAmount;
            }
            
            // Update amount due field
            const amountDueField = document.getElementById('amountDueField');
            if (amountDueField) {
                // Only reset if not currently focused
                if (document.activeElement !== amountDueField) {
                    amountDueField.value = totalAmount.toFixed(2);
                    amountDueField.dataset.originalTotal = totalAmount;
                }
                validateAmountDue(amountDueField.value, totalAmount);
            }

            // Update payment button state
            const payBtn = document.querySelector('button[name="process_payment"]');
            if (payBtn) {
                payBtn.disabled = totalAmount === 0;
            }
        });
}

function bindPrescriptionRequirement() {
    const requiresPrescription = document.getElementById('requiresPrescription')?.value === 'true';
    const customerField = document.querySelector('[name="customer_name"]');
    
    if (customerField) {
        customerField.required = requiresPrescription;
        customerField.placeholder = requiresPrescription 
            ? "Required for prescription items" 
            : "Optional";
    }
}

function refreshPaymentForm() {
    const form = document.getElementById('paymentForm');
    if (!form) return;

    // Reset customer field
    const nameField = form.querySelector('[name="customer_name"]');
    if (nameField) {
        nameField.value = '';
        nameField.classList.remove('is-invalid');
    }

    // Reset validation messages
    form.querySelectorAll('.invalid-feedback').forEach(span => span.remove());
    
    // Reset amount due to current total
    updateCartTotal();
}

// ------------------ UI Feedback ------------------
function clearFeedback(containerId) {
    const container = document.getElementById(containerId);
    if (container) container.innerHTML = '';
}

function showInlineAlert(message, type = 'info') {
    const feedback = document.getElementById('cartFeedback');
    if (!feedback) return;

    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show mt-2`;
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
    `;
    feedback.appendChild(alertDiv);
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toastEl = document.createElement('div');
    toastEl.className = `toast align-items-center text-bg-${type}`;
    toastEl.setAttribute('role', 'alert');
    toastEl.setAttribute('aria-live', 'assertive');
    toastEl.setAttribute('aria-atomic', 'true');

    toastEl.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">${message}</div>
            <button type="button" class="btn-close me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
        </div>
    `;

    container.appendChild(toastEl);
    const toast = new bootstrap.Toast(toastEl, { delay: 3000 });
    toast.show();

    toastEl.addEventListener('hidden.bs.toast', () => {
        toastEl.remove();
    });
}

function initToastContainer() {
    if (!document.getElementById('toastContainer')) {
        const container = document.createElement('div');
        container.id = 'toastContainer';
        container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
        document.body.appendChild(container);
    }
}