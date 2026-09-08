-- Parent/child job relationships.
-- A product_scan parent can wait for its chemical_research children.

CREATE TABLE IF NOT EXISTS job_dependency (
  parent_job_id VARCHAR(64) NOT NULL,
  child_job_id  VARCHAR(64) NOT NULL,
  PRIMARY KEY (parent_job_id, child_job_id),
  KEY ix_job_dependency_child (child_job_id),
  CONSTRAINT fk_job_dependency_parent
    FOREIGN KEY (parent_job_id) REFERENCES job(job_id) ON DELETE CASCADE,
  CONSTRAINT fk_job_dependency_child
    FOREIGN KEY (child_job_id) REFERENCES job(job_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
