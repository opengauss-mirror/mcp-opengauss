package org.opengauss.migrationmcp.entity;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 数据迁移任务
 * MySQL数据库到openGauss数据库的数据迁移任务
 * 源端数据库到目标端数据库的数据迁移任务
 */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class MigrationTask {
    // 迁移任务ID
    @JsonProperty("id")
    private String taskId;

    // 迁移任务名称
    private String taskName;

    // 迁移任务执行状态，（0：未执行；1：执行中；2：已完成；）
    @JsonProperty("execStatus")
    private int executeStatus;

    // 源端数据库所在服务器ip
    @JsonProperty("sourceDbHost")
    String sourceDatabaseIp;

    // 源端数据库端口
    @JsonProperty("sourceDbPort")
    String sourceDatabasePort;

    // 源端数据库database名
    @JsonProperty("sourceDb")
    String sourceDatabaseName;

    // 目标端数据库所在服务器ip
    @JsonProperty("targetDbHost")
    String targetDatabaseIp;

    // 目标端数据库端口
    @JsonProperty("targetDbPort")
    String targetDatabasePort;

    // 目标端数据库database名
    @JsonProperty("targetDb")
    String targetDatabaseName;
}
