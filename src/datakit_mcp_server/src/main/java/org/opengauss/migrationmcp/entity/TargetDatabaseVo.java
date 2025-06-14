package org.opengauss.migrationmcp.entity;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 目标端数据库信息
 * 目标端数据库类型为MySQL
 */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class TargetDatabaseVo {
    // 数据库ID
    @JsonProperty("nodeId")
    private String id;

    // 数据库所在主机ip
    @JsonProperty("publicIp")
    private String ip;

    // 数据库端口
    @JsonProperty("dbPort")
    private String port;

    // 数据库用户名
    @JsonProperty("dbUser")
    private String username;

    // 用户密码
    @JsonProperty("dbUserPassword")
    private String password;

    // 连接数据库使用的默认database
    @JsonProperty("dbName")
    private String databaseName;

    // 数据所在主机的ssh端口
    private String hostPort;
}
