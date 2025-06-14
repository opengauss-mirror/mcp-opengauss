package org.opengauss.migrationmcp.entity;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

/**
 * 源端数据库信息
 * 源端数据库类型为MySQL
 */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class SourceDatabaseVo {
    // 数据库ID
    @JsonProperty("clusterNodeId")
    private String id;

    // 数据库所在主机ip
    private String ip;

    // 数据库端口
    private String port;

    // 数据库用户名
    private String username;

    // 用户密码
    private String password;

    // 连接数据库的url
    private String url;
}
