SELECT p.*, e.delta, e.percent, e.old_price, first.price AS first_price,
       p.price - first.price AS total_delta
FROM products p
LEFT JOIN change_events e ON e.product_id = p.id AND e.run_id = ?
    AND e.kind IN ('increased', 'decreased')
LEFT JOIN product_snapshots first ON first.product_id = p.id AND first.run_id = (
    SELECT s.run_id
    FROM product_snapshots s
    JOIN crawl_runs r ON r.id = s.run_id AND r.status = 'success'
    WHERE s.product_id = p.id AND s.present = 1 AND s.price IS NOT NULL
    ORDER BY s.run_id
    LIMIT 1
)
WHERE {where}
ORDER BY {order}
LIMIT ? OFFSET ?;
