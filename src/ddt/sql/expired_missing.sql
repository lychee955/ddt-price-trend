SELECT p.id
FROM products p
WHERE p.status IN ('suspected_missing', 'missing')
  AND julianday((
      SELECT s.observed_at
      FROM product_snapshots s
      WHERE s.product_id = p.id
        AND s.source_status IN ('suspected_missing', 'missing')
        AND s.run_id > COALESCE((
            SELECT previous.run_id
            FROM product_snapshots previous
            WHERE previous.product_id = p.id
              AND previous.source_status NOT IN ('suspected_missing', 'missing')
            ORDER BY previous.run_id DESC
            LIMIT 1
        ), 0)
      ORDER BY s.run_id
      LIMIT 1
  )) <= julianday(?) - 21;
