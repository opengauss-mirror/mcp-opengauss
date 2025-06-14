package org.opengauss.migrationmcp.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

@Data
public class TargetDataBaseDto {
    private String dbName;

    @JsonProperty("isSelect")
    private boolean isSelect;
}
