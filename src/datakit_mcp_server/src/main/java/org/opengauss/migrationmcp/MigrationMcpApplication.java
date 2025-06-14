package org.opengauss.migrationmcp;

import org.opengauss.migrationmcp.service.DatabaseService;
import org.opengauss.migrationmcp.service.MigrationService;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.ToolCallbacks;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

import java.util.List;

@SpringBootApplication
public class MigrationMcpApplication {

    public static void main(String[] args) {
        SpringApplication.run(MigrationMcpApplication.class, args);
    }

    @Bean
    public List<ToolCallback> getToolCallbacks(DatabaseService databaseService, MigrationService migrationService) {
        return List.of(ToolCallbacks.from(databaseService, migrationService));
    }
}
