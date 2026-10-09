-- 原始 UUID 结构保留为历史迁移；本次在同一事务中转换编号并重新校验全部外键。
-- 不修改密码、会话令牌、原始 JSON、正文、时间戳及审计不可变触发器。
CREATE FUNCTION pg_temp.compact_id(value uuid) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    -- 固定演示编号的类型前缀和末尾序号，用于生成可读的 D/U/E/P/T 编号。
    canonical text := value::text;
    kind text;
    employee_number integer;
    day_number integer;
BEGIN
    IF canonical ~ '^[1-5]0000000-0000-0000-0000-00000000000[1-9]$' THEN
        kind := substr('DUEPT', substr(canonical,1,1)::integer, 1);
        RETURN kind || right(canonical,1);
    END IF;
    FOR employee_number IN 1..5 LOOP
        FOR day_number IN 0..4 LOOP
            IF value = md5('30000000-0000-0000-0000-00000000000' || employee_number::text ||
                           ('2026-09-28'::date + day_number)::text)::uuid THEN
                RETURN 'A' || ((employee_number-1)*5+day_number+1)::text;
            END IF;
        END LOOP;
    END LOOP;
    RETURN translate(rtrim(encode(decode(substr(md5(canonical),1,24),'hex'),'base64'),'='),'+/','-_');
END;
$$;

-- 全库映射带唯一约束；即使发生极小概率压缩碰撞，也会在修改任何字段前中止事务。
CREATE TEMP TABLE id_conversion(old_id uuid PRIMARY KEY, short_id text NOT NULL UNIQUE) ON COMMIT DROP;
CREATE TEMP TABLE saved_foreign_keys(table_name text, constraint_name text, definition text) ON COMMIT DROP;
INSERT INTO saved_foreign_keys
SELECT c.conrelid::regclass::text,c.conname,pg_get_constraintdef(c.oid)
FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
WHERE c.contype='f' AND n.nspname IN ('app_system','app_runtime','app_knowledge','app_memory','app_audit','demo_business');

CREATE FUNCTION pg_temp.convert_ids(values_to_convert uuid[]) RETURNS varchar(16)[]
LANGUAGE sql IMMUTABLE STRICT AS $$
    -- 接收 UUID 数组，按原顺序转换并保留空数组，返回短字符串数组。
    SELECT COALESCE(array_agg(pg_temp.compact_id(value)::varchar(16) ORDER BY position), '{}')
    FROM unnest(values_to_convert) WITH ORDINALITY AS ids(value,position);
$$;

DO $$
DECLARE
    -- 保存字段与外键元数据，先验证映射，后移除依赖、转换字段并恢复外键。
    field record;
    relation record;
BEGIN
    FOR field IN SELECT table_schema,table_name,column_name,udt_name FROM information_schema.columns
        WHERE table_schema IN ('app_system','app_runtime','app_knowledge','app_memory','app_audit','demo_business')
        AND udt_name IN ('uuid','_uuid')
    LOOP
        IF field.udt_name='uuid' THEN
            EXECUTE format('INSERT INTO id_conversion SELECT DISTINCT %1$I,pg_temp.compact_id(%1$I) FROM %2$I.%3$I WHERE %1$I IS NOT NULL ON CONFLICT(old_id) DO NOTHING',
                           field.column_name,field.table_schema,field.table_name);
        ELSE
            EXECUTE format('INSERT INTO id_conversion SELECT DISTINCT value,pg_temp.compact_id(value) FROM %I.%I CROSS JOIN LATERAL unnest(%I) value WHERE value IS NOT NULL ON CONFLICT(old_id) DO NOTHING',
                           field.table_schema,field.table_name,field.column_name);
        END IF;
    END LOOP;
    FOR relation IN SELECT * FROM saved_foreign_keys LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',relation.table_name,relation.constraint_name);
    END LOOP;
    FOR field IN SELECT table_schema,table_name,column_name,udt_name,column_default FROM information_schema.columns
        WHERE table_schema IN ('app_system','app_runtime','app_knowledge','app_memory','app_audit','demo_business')
        AND udt_name IN ('uuid','_uuid') ORDER BY table_schema,table_name,ordinal_position
    LOOP
        IF field.udt_name='uuid' THEN
            EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I TYPE varchar(16) USING pg_temp.compact_id(%I)',
                           field.table_schema,field.table_name,field.column_name,field.column_name);
        ELSE
            EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I DROP DEFAULT',field.table_schema,field.table_name,field.column_name);
            EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I TYPE varchar(16)[] USING pg_temp.convert_ids(%I)',
                           field.table_schema,field.table_name,field.column_name,field.column_name);
            IF field.column_default IS NOT NULL THEN
                EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I SET DEFAULT ''{}''::varchar(16)[]',
                               field.table_schema,field.table_name,field.column_name);
            END IF;
        END IF;
    END LOOP;
    FOR relation IN SELECT * FROM saved_foreign_keys LOOP
        EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s',relation.table_name,relation.constraint_name,relation.definition);
    END LOOP;
END;
$$;
