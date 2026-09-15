-- Split the single review-control setting into one per scan type.
--
-- 'review_status_control' governed only the chemical/product review gate
-- (packages/jobs/handlers.py::research_chemical). Now that restaurant
-- analysis gets its own admin-controlled row, the product key is renamed so
-- neither name is ambiguous about which scan type it governs.
--
-- The UPDATE is naturally idempotent: on a second run WHERE setting_key =
-- 'review_status_control' matches nothing, since the first run already
-- renamed it.
UPDATE admin_setting SET setting_key = 'review_status_control_of_product'
  WHERE setting_key = 'review_status_control';

-- Covers a fresh database too, where 005_admin_review already seeded the old
-- key under 001-006 and this migration's UPDATE just renamed it above — this
-- INSERT IGNORE is then a no-op. It only does real work if 005's seed row is
-- ever missing.
INSERT IGNORE INTO admin_setting (setting_key, value, updated_by)
VALUES ('review_status_control_of_product', 'manual', 'migration');

INSERT IGNORE INTO admin_setting (setting_key, value, updated_by)
VALUES ('review_status_control_of_restaurant', 'manual', 'migration');
