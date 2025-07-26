document.addEventListener("DOMContentLoaded", () => {
    const productSearchInput = document.getElementById('productSearch');
    const searchResults = document.getElementById('searchResults');
    const hiddenProduct = document.getElementById('product');
    
    let debounceTimeout = null;
    let abortController = null;

    // Event Delegation for search results
    searchResults.addEventListener('click', (e) => {
        if (e.target.classList.contains('list-group-item-action')) {
            e.preventDefault();
            const productId = e.target.dataset.productId;
            const productName = e.target.dataset.productName;
            
            productSearchInput.value = productName;
            hiddenProduct.value = productId;
            hideResults();
        }
    });

    // Input handler with optimized debounce and abort
    productSearchInput.addEventListener('input', () => {
        const query = productSearchInput.value.trim();
        
        // Clear previous requests and timeouts
        if (abortController) abortController.abort();
        clearTimeout(debounceTimeout);
        hideResults();

        // Validate minimum query length
        if (query.length < 2) return;

        debounceTimeout = setTimeout(() => {
            abortController = new AbortController();
            fetchSearchResults(query, abortController.signal);
        }, 300);
    });

    // Hide results when clicking outside
    document.addEventListener('click', (e) => {
        if (!productSearchInput.contains(e.target) && !searchResults.contains(e.target)) {
            hideResults();
        }
    });

    // Show existing results on focus
    productSearchInput.addEventListener('focus', () => {
        if (searchResults.children.length > 0) {
            searchResults.style.display = 'block';
        }
    });

    // Helper functions
    function hideResults() {
        searchResults.innerHTML = '';
        searchResults.style.display = 'none';
    }

    async function fetchSearchResults(query, signal) {
        try {
            const url = `${productSearchInput.dataset.url}?q=${encodeURIComponent(query)}`;
            const response = await fetch(url, { signal });
            
            if (!response.ok) throw new Error('Network response error');
            const data = await response.json();

            if (!Array.isArray(data.results)) return;
            
            if (data.results.length > 0) {
                searchResults.style.display = 'block';
                data.results.forEach(product => {
                    const item = document.createElement('a');
                    item.href = "#";
                    item.className = 'list-group-item list-group-item-action';
                    item.textContent = `${product.name}${product.dosage ? ` (${product.dosage})` : ''}`;
                    item.dataset.productId = product.id;
                    item.dataset.productName = product.name;  // Store name separately
                    
                    searchResults.appendChild(item);
                });
            }
        } catch (error) {
            if (error.name !== 'AbortError') {
                console.error('Fetch error:', error);
            }
        }
    }
});