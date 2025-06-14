package org.opengauss.migrationmcp.entity;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import lombok.Data;

import java.util.List;

/**
 * 源端数据库集群
 * 源端数据库类型为MySQL
 */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class SourceDatabaseClusterVo {
    // 集群ID
    private String clusterId;

    // 集群名称
    private String name;

    // 集群的节点列表
    private List<SourceDatabaseVo> nodes;
}
