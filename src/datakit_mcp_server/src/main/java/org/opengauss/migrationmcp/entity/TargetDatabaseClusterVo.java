package org.opengauss.migrationmcp.entity;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import lombok.Data;

import java.util.List;

/**
 * 目标端数据库集群
 * 目标端数据库类型为openGauss
 */
@Data
@JsonIgnoreProperties(ignoreUnknown = true)
public class TargetDatabaseClusterVo {
    // 集群ID
    private String clusterId;

    // 集群名称
    private String clusterName;

    // 集群的数据库节点列表
    private List<TargetDatabaseVo> clusterNodes;
}
